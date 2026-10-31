# -*- coding: utf-8 -*-
"""
injection_marker_gui.py
=======================
在光纤记录（Fluorescence.csv + Events.csv）中自动定位"注射时刻"的运动伪影，
在数据中写入 marker，并输出一张诊断图。原始数据只读，全部结果写入源目录下的
injection_marker/ 子文件夹。

判据（与已有分析一致）：
  1) 伪影指数 = 四通道高频能量（2 s 滑动 std）除各自基线后取平均；
  2) 事件 = 指数 > 阈值且持续 >= 最短时长；
  3) 注射候选窗 = 指定时间范围（默认 30-90 min）内最强的事件簇。

用法：
  GUI     python injection_marker_gui.py
  命令行  python injection_marker_gui.py --nogui --fluo <Fluorescence.csv> \
              --events <Events.csv> --outdir <dir> [--start 30 --end 90 --thr 3]

输出的 Fluorescence.csv：原文件逐行照抄，仅在 marker 时刻所在行的 Events 列写入名字。
输出的 Events.csv      ：原内容保留，追加注射 marker 行（时间单位 ms，与原生文件一致）。
"""

import os
import sys
import csv
import argparse
import subprocess
import threading
import traceback
import queue

# Platform choices used both by the import guard below and by the GUI/figure
# code further down, so they have to be defined before the imports are tried.
if sys.platform == "darwin":
    # PingFang.ttc is a font collection Pillow cannot open, so the CJK
    # candidates here are the ones that actually load on macOS.
    FONT_CANDIDATES = [
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ]
    FONT_BOLD = FONT_CANDIDATES[0]
    UI_FONT = "PingFang SC"
    MONO_FONT = "Menlo"
    LAUNCHER_HINT = "run_InjectionFinder.command"
else:
    FONT_CANDIDATES = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
    UI_FONT = "Microsoft YaHei"
    MONO_FONT = "Consolas"
    LAUNCHER_HINT = "run_InjectionFinder.bat"


def _is_gui_run():
    """True when this process is going to open a window.

    A command-line run must never raise a dialog: the box is modal, so it
    would block the caller until someone clicks it, and the same message is
    already on stderr.  --nogui or --fluo mean the run is scripted.
    """
    argv = sys.argv[1:]
    if "--nogui" in argv:
        return False
    return not any(arg == "--fluo" or arg.startswith("--fluo=") for arg in argv)

try:
    import numpy as np
    import pandas as pd
    from PIL import Image, ImageDraw, ImageFont
except ImportError as _exc:        # 缺第三方库：给中文可执行指引，而不是裸 traceback
    _msg = (
        "缺少运行依赖：%s\n\n"
        "请双击本目录下的 " + LAUNCHER_HINT + " 自动安装，\n"
        "或在本机命令行执行：\n"
        "    \"%s\" -m pip install numpy pandas pillow\n"
        % (_exc, sys.executable)
    )
    print(_msg, file=sys.stderr)
    if _is_gui_run():
        try:
            import tkinter.messagebox as _mb
            _mb.showerror("InjectionFinder 缺少依赖", _msg)
        except Exception:
            pass
    sys.exit(1)


def open_in_file_manager(path):
    """Reveal a folder in Explorer, Finder or the desktop's file manager.

    os.startfile is Windows-only; on macOS the attribute does not exist.
    """
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])

ANALYSIS_FS = 10.0          # 重采样目标采样率 (Hz)
DETREND_SEC = 300.0         # 去漂移滑动中位数窗长 (s)
HF_SEC = 2.0                # 高频能量窗长 (s)
MIN_GAP_SEC = 0.5           # 事件段合并间隔 (s)
CLUSTER_GAP_SEC = 60.0      # 事件合并成"簇"的最大间隔 (s)


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------
def read_fluorescence(path):
    """返回 (ts_raw, channels)，均为 numpy 数组。

    以二进制逐行读取：编码、BOM、换行方式都与源文件无关，解析只针对 ASCII 数值字段。
    ts_raw 单位可能是 ms 或 s，交由 to_seconds() 统一。
    """
    with open(path, "rb") as fh:
        fh.readline()                       # 元数据行
        fh.readline()                       # 表头
        ts, ch = [], []
        for line in fh:
            if not line.strip():
                continue
            f = line.rstrip(b"\r\n").split(b",")
            if len(f) < 6:
                continue
            try:
                # 逐字段转换：任一通道为空/非数值则该采样点整体丢弃，保证 ts 与 ch 等长
                v = [float(f[0]), float(f[2]), float(f[3]), float(f[4]), float(f[5])]
            except ValueError:
                continue
            ts.append(v[0])
            ch.append(v[1:])
    if not ts:
        raise ValueError("Fluorescence.csv 中没读到有效数据行。")
    return np.asarray(ts, float), np.asarray(ch, float)


def read_events(path):
    """读 Events.csv -> list[list[str]]，逐行保留原文，安全处理空文件/缺失。"""
    rows = []
    if not path or not os.path.isfile(path):
        return rows
    with open(path, "rb") as fh:
        for line in fh:
            if line.strip():
                rows.append(line.rstrip(b"\r\n").decode("utf-8", "replace").split(","))
    return rows


def to_seconds(ts_raw, unit="auto"):
    """把时间戳统一成秒。unit: auto / ms / s。NaN 原样保留。"""
    a = np.asarray(ts_raw, dtype=float)
    if unit == "ms":
        return a / 1000.0
    if unit == "s":
        return a.copy()
    v = a[~np.isnan(a)]
    if v.size < 2 or float(v[-1] - v[0]) <= 0:
        return a / 1000.0
    dt = float(v[-1] - v[0]) / (v.size - 1)
    if 1.0 < dt < 1000.0:        # 典型 ms：dt≈100
        return a / 1000.0
    return a.copy()              # 已是秒


# --------------------------------------------------------------------------
# 分析
# --------------------------------------------------------------------------
def compute_artifact_index(ch4, t_s):
    """四通道归一化高频能量平均 = 伪影指数。返回 (index, hf_dict, t_uniform)。"""
    fs = ANALYSIS_FS
    n = int(round((t_s[-1] - t_s[0]) * fs)) + 1
    tu = t_s[0] + np.arange(n) / fs
    hp = {}
    rstd = {}
    for i, nm in enumerate(["CH1_410", "CH1_470", "CH2_410", "CH2_470"]):
        y = np.interp(tu, t_s, ch4[:, i])
        s = pd.Series(y)
        base = s.rolling(int(DETREND_SEC * fs), center=True, min_periods=1).median()
        h = (s - base).to_numpy()
        hp[nm] = h
        rstd[nm] = pd.Series(h).rolling(int(HF_SEC * fs), center=True,
                                        min_periods=1).std().bfill().ffill().to_numpy()
    stack = np.vstack([rstd[k] for k in rstd])
    med = np.median(stack, axis=1, keepdims=True)
    med[med <= 0] = 1e-9
    idx = (stack / med).mean(axis=0)
    return idx, hp, tu


def detect_events(idx, t, thr, min_dur):
    """返回 [(start_s, end_s, peak, peak_t, dur_s), ...]。"""
    mask = idx > thr
    if not mask.any():
        return []
    d = np.diff(mask.astype(np.int8))
    starts = list(np.where(d == 1)[0] + 1)
    ends = list(np.where(d == -1)[0] + 1)
    if mask[0]:
        starts.insert(0, 0)
    if mask[-1]:
        ends.append(len(mask))
    dt = float(t[1] - t[0]) if len(t) > 1 else 0.0
    ev = []
    for s, e in zip(starts, ends):
        a, b = t[s], t[e - 1]
        dur = (e - s) * dt            # 段内采样点数 × 采样间隔，避免差一个间隔
        if dur < min_dur:
            continue
        seg = idx[s:e]
        k = int(np.argmax(seg))
        ev.append((a, b, float(seg[k]), float(t[s + k]), dur))
    # 合并间隔 < MIN_GAP_SEC 的事件段
    merged = []
    for e in ev:
        if merged and e[0] - merged[-1][1] < MIN_GAP_SEC:
            prev = merged[-1]
            best = prev if prev[2] >= e[2] else e
            a0 = min(prev[0], e[0])
            b0 = max(prev[1], e[1])
            merged[-1] = (a0, b0, best[2], best[3], b0 - a0)
        else:
            merged.append(e)
    return merged


def pick_candidate(events, win_start_s, win_end_s):
    """在时间窗内取最强事件簇，返回 (win_start, win_end, peak, peak_t, n_events)
    或 None。返回的起止时间已裁剪到搜索窗口内。"""
    inside = [e for e in events if e[0] <= win_end_s and e[1] >= win_start_s]
    if not inside:
        return None
    inside.sort(key=lambda e: e[0])
    clusters = []
    cur = [inside[0]]
    for e in inside[1:]:
        if e[0] - cur[-1][1] < CLUSTER_GAP_SEC:
            cur.append(e)
        else:
            clusters.append(cur)
            cur = [e]
    clusters.append(cur)
    best = max(clusters, key=lambda c: max(x[2] for x in c))
    peak_ev = max(best, key=lambda x: x[2])
    a = max(best[0][0], win_start_s)      # 裁剪到搜索窗口，marker 不越界
    b = min(best[-1][1], win_end_s)
    return (a, b, peak_ev[2], peak_ev[3], len(best))


# --------------------------------------------------------------------------
# 写出
# --------------------------------------------------------------------------
def _split_eol(line):
    """把一行 bytes 拆成 (内容, 行尾字节)，保留源文件的换行风格。"""
    if line.endswith(b"\r\n"):
        return line[:-2], b"\r\n"
    if line.endswith(b"\n"):
        return line[:-1], b"\n"
    return line, b""


def write_fluorescence(src, dst, markers, time_unit="auto", warn=None):
    """markers: [(t_s, name), ...]。

    二进制逐行照抄原文件（元数据行 + 表头 + 全部数据行），仅把 marker 时刻所在
    数据行的 Events 字段替换为 marker 名；其余字节、行数、编码、换行方式不变。
    返回 [(name, 实际命中采样点的时间秒), ...]，供 Events.csv 使用同一时刻。
    """
    if os.path.abspath(src).lower() == os.path.abspath(dst).lower():
        raise RuntimeError("拒绝写入：输出路径与源文件相同（%s）。原始数据只读。" % dst)

    # 第一遍：按"非空行"计数收集时间戳，与第二遍的行号口径完全一致
    with open(src, "rb") as fh:
        fh.readline()                       # 元数据行
        fh.readline()                       # 表头
        ts = []
        for line in fh:
            if not line.strip():
                continue
            try:
                ts.append(float(line.split(b",", 1)[0]))
            except ValueError:
                ts.append(np.nan)           # 坏行保留占位，保证行号对齐
    if not ts:
        raise ValueError("Fluorescence.csv 中没有数据行。")
    t_src = to_seconds(np.asarray(ts, float), time_unit)

    targets = {}
    hits = []
    for t_mark, name in markers:
        try:
            j = int(np.nanargmin(np.abs(t_src - t_mark)))
        except ValueError:                  # 全是 NaN
            continue
        targets[j] = name
        hits.append((name, float(t_src[j])))

    # 第二遍：原样写出，只改目标行的 Events 字段
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(fin.readline())          # 元数据行，原样字节
        fout.write(fin.readline())          # 表头，原样字节
        k = 0
        for line in fin:
            if not line.strip():
                fout.write(line)            # 空行原样，不占行号
                continue
            if k in targets:
                body, eol = _split_eol(line)
                f = body.split(b",")
                if len(f) >= 2:
                    old = f[1].strip()
                    f[1] = targets[k].encode("utf-8")
                    if old and warn is not None:
                        warn("      [警告] 数据行 %d 原有 Events 值 '%s' 被 '%s' 覆盖。"
                             % (k, old.decode("utf-8", "replace"), targets[k]))
                fout.write(b",".join(f) + (eol or b"\n"))
            else:
                fout.write(line)
            k += 1
    return hits


def write_events(src, dst, hits):
    """原 Events.csv 内容保留，追加 marker 行（单位 ms）。

    hits: [(name, 实际命中采样点时间秒), ...]，时间戳与 Fluorescence.csv 里的行完全一致。
    """
    rows = read_events(src)
    eol = "\n"
    if src and os.path.isfile(src):
        with open(src, "rb") as fh:
            if b"\r\n" in fh.read(8192):
                eol = "\r\n"
    lines = []
    if rows:
        lines.append(",".join(rows[0]))
        lines.extend(",".join(r) for r in rows[1:])
    else:
        lines.append("TimeStamp,Name,State")
    for name, t_s in hits:
        lines.append("%.3f,%s,1" % (t_s * 1000.0, name))
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        fh.write(eol.join(lines) + eol)


# --------------------------------------------------------------------------
# 出图
# --------------------------------------------------------------------------
def _font(size, bold=False):
    # A candidate can exist but still fail to open (a font collection, or a
    # broken file), so each one is tried instead of assuming the first match
    # works; the PIL default is the last resort rather than the first.
    for p in ((FONT_BOLD,) if bold else ()) + tuple(FONT_CANDIDATES):
        if not p or not os.path.isfile(p):
            continue
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make_figure(out_png, t, idx, hp, cand,
                motion=None, title_tag="", dpi=2):
    """cand = (ws, we, peak, peak_t, n_ev)；motion=(t_s_arr, motion_arr) 可选。"""
    if len(t) < 2 or not np.isfinite(t[-1]) or t[-1] <= t[0]:
        raise ValueError("数据时长无效，无法出图。")
    has_c = motion is not None
    W, H = 1560, (900 if has_c else 660)
    S = dpi
    img = Image.new("RGB", (W * S, H * S), "white")
    d = ImageDraw.Draw(img)
    f10, f11, f13 = _font(10 * S), _font(11 * S), _font(13 * S, True)
    total_min = t[-1] / 60.0

    def sx(v, x0, x1, p0, p1):
        return p0 + (v - x0) / (x1 - x0) * (p1 - p0)

    def sy(v, y0, y1, p0, p1):
        return p0 + (v - y0) / (y1 - y0) * (p1 - p0)

    def frame(box, x0, x1, y0, y1, xticks, yticks, xfmt, yfmt, xl, yl):
        L, T, R, B = box
        for xt in xticks:
            px = sx(xt, x0, x1, L, R)
            d.line([(px * S, B * S), (px * S, (B + 5) * S)], fill="black", width=S)
            d.line([(px * S, T * S), (px * S, B * S)], fill=(236, 236, 236), width=S)
            d.text((px * S, (B + 7) * S), xfmt(xt), font=f10, fill="black", anchor="ma")
        for yt in yticks:
            py = sy(yt, y0, y1, B, T)
            d.line([(L * S, py * S), ((L - 5) * S, py * S)], fill="black", width=S)
            d.text(((L - 8) * S, py * S), yfmt(yt), font=f10, fill="black", anchor="rm")
        d.line([(L * S, B * S), (R * S, B * S)], fill="black", width=S)
        d.line([(L * S, T * S), (L * S, B * S)], fill="black", width=S)
        d.text(((L + R) / 2 * S, (B + 26) * S), xl, font=f11, fill="black", anchor="ma")
        if yl:
            d.text(((L - 46) * S, (T + B) / 2 * S), yl, font=f11, fill="black", anchor="mm")

    # ---------- A: 全程 ----------
    P1 = (95, 62, 1520, 288)
    x0, x1 = 0, total_min
    yl0, yl1 = 0, 2.05
    # 预设注射窗口底色（30–90 min），仅作视觉参考；记录短于窗口时不画
    wa, wb = 30.0, 90.0
    wb_eff = min(wb, x1)
    if yl1 > yl0 and wa < wb_eff:
        xa, xb = sx(wa, x0, x1, P1[0], P1[2]), sx(wb_eff, x0, x1, P1[0], P1[2])
        d.rectangle([xa * S, P1[1] * S, xb * S, P1[3] * S], fill=(255, 247, 224))
    frame(P1, x0, x1, yl0, yl1, list(range(0, int(total_min) + 1, 120)),
          [0, 0.301, 0.699, 1.0, 1.301, 1.699], lambda v: "%d" % v,
          lambda v: {0: "1", 0.301: "2", 0.699: "5", 1.0: "10", 1.301: "20", 1.699: "50"}[round(v, 3)],
          "记录时间 (min)", "")
    d.text(((P1[0] + P1[2]) / 2 * S, (P1[1] - 40) * S),
           "A  全程 %.2f h 运动伪影指数%s" % (t[-1] / 3600.0, title_tag), font=f13, fill="black", anchor="ma")
    if wa < wb_eff:
        d.text((sx(wa, x0, x1, P1[0], P1[2]) * S, (P1[1] + 4) * S), "预设窗口 30–90 min",
               font=_font(10 * S), fill=(150, 110, 20))
    # 记录末尾（通常为取下光纤）的说明
    tail = t[-1] / 60.0
    if tail > 600:
        d.text(((P1[2] - 6) * S, (P1[1] + 30) * S), "记录结束\n(取下光纤)", font=_font(10 * S),
               fill=(120, 120, 120), anchor="ra")
    nb = len(t) // (int(10 * ANALYSIS_FS)) or 1
    tb = t[:nb * (int(10 * ANALYSIS_FS))].reshape(nb, -1).mean(axis=1) / 60.0
    ib = idx[:nb * (int(10 * ANALYSIS_FS))].reshape(nb, -1).mean(axis=1)
    ys = np.clip(np.log10(np.clip(ib, 1, None)), yl0, yl1)
    d.line([(sx(a, x0, x1, P1[0], P1[2]) * S, sy(b, yl0, yl1, P1[3], P1[1]) * S) for a, b in zip(tb, ys)],
           fill=(31, 119, 180), width=S)
    if cand is not None:
        px = sx(cand[3] / 60.0, x0, x1, P1[0], P1[2])
        d.line([(px * S, P1[1] * S), (px * S, P1[3] * S)], fill=(214, 39, 40), width=2 * S)
        d.text(((px + 8) * S, (P1[1] + 42) * S),
               "%.2f–%.2f min\n伪影指数峰值 %.1f×基线" % (cand[0] / 60.0, cand[1] / 60.0, cand[2]),
               font=_font(11 * S, True), fill=(214, 39, 40))

    # ---------- B: 放大 ----------
    center = (cand[0] + cand[1]) / 2.0 if cand else (min(80 * 60, t[-1] * 0.5))
    half = max(180.0, (cand[1] - cand[0]) * 3.0 if cand else 180.0)
    z0 = max(0.0, center - half)
    z1 = min(t[-1], center + half)
    if z1 - z0 < 1.0:                    # 兜底：放大窗至少有 1 s
        z0, z1 = max(0.0, t[-1] - 60.0), t[-1]
    P2 = (95, 385, 1520, 610)
    sel = (t >= z0) & (t <= z1)
    ymax = 1.30
    if sel.any():
        ymax = max(0.3, float(np.nanmax([np.abs(hp[k][sel]).max() for k in
                                         ("CH1_470", "CH2_470")])) * 1.15)
    frame(P2, z0, z1, -ymax * 0.3, ymax,
          list(range(int(np.ceil(z0 / 60.0)), int(z1 / 60.0) + 1, max(1, int((z1 - z0) / 60 / 8)))),
          [-ymax * 0.3, 0, ymax / 2, ymax], lambda v: "%.1f" % (v / 60.0), lambda v: "%.1f" % v,
          "记录时间 (min)", "")
    d.text(((P2[0] + P2[2]) / 2 * S, (P2[1] - 40) * S),
           "B  放大 %.1f–%.1f min：410/470 四通道同步同幅抖动 → 运动伪影%s"
           % (z0 / 60.0, z1 / 60.0, ("（候选窗 %.2f–%.2f min）" % (cand[0] / 60.0, cand[1] / 60.0)) if cand else ""),
           font=f13, fill="black", anchor="ma")
    xs_z = t[sel]
    for key, col, wdt in [("CH1_470", (214, 39, 40), 2), ("CH2_470", (31, 119, 180), 2),
                          ("CH1_410", (255, 152, 150), 1), ("CH2_410", (140, 190, 235), 1)]:
        y = hp[key][sel]
        d.line([(sx(a, z0, z1, P2[0], P2[2]) * S, sy(b, -ymax * 0.3, ymax, P2[3], P2[1]) * S)
                for a, b in zip(xs_z, y)], fill=col, width=wdt * S)
    if cand is not None:
        d.rectangle([sx(cand[0], z0, z1, P2[0], P2[2]) * S, P2[1] * S,
                     sx(cand[1], z0, z1, P2[0], P2[2]) * S, P2[3] * S],
                    outline=(255, 140, 0), width=2 * S)
    for i, (nm, col) in enumerate([("CH1-470", (214, 39, 40)), ("CH2-470", (31, 119, 180)),
                                   ("CH1-410", (235, 120, 120)), ("CH2-410", (110, 160, 215))]):
        yy = P2[1] + 14 + i * 17
        d.line([((P2[2] - 150) * S, yy * S), ((P2[2] - 120) * S, yy * S)], fill=col, width=2 * S)
        d.text(((P2[2] - 115) * S, yy * S), nm, font=f10, fill=col, anchor="lm")

    # ---------- C: 行为视频（可选） ----------
    if has_c:
        mt, mv = motion
        P3 = (95, 712, 1520, 838)
        mmax = 1.0
        selm = (mt >= z0) & (mt <= z1)
        if selm.any():
            mmax = max(1.0, float(np.nanmax(mv[selm])) * 1.1)
        frame(P3, z0, z1, 0, mmax,
              list(range(int(np.ceil(z0 / 60.0)), int(z1 / 60.0) + 1, max(1, int((z1 - z0) / 60 / 8)))),
              [0, mmax / 4, mmax / 2, mmax * 3 / 4, mmax],
              lambda v: "%.1f" % (v / 60.0), lambda v: "%.1fk" % (v / 1000.0),
              "记录时间 (min)", "")
        d.text(((P3[0] + P3[2]) / 2 * S, (P3[1] - 40) * S),
               "C  同步行为视频运动量（与光纤同段对齐）", font=f13, fill="black", anchor="ma")
        d.line([(sx(a, z0, z1, P3[0], P3[2]) * S, sy(b, 0, mmax, P3[3], P3[1]) * S)
                for a, b in zip(mt[selm], mv[selm])], fill=(110, 110, 110), width=S)
        if cand is not None:
            d.rectangle([sx(cand[0], z0, z1, P3[0], P3[2]) * S, P3[1] * S,
                         sx(cand[1], z0, z1, P3[0], P3[2]) * S, P3[3] * S],
                        outline=(255, 140, 0), width=2 * S)

    img.resize((W, H), Image.LANCZOS).save(out_png, optimize=True)
    return out_png


def load_motion(path, duration_s, log=print, fs=1.0):
    """读 Any-maze FreezingOutput.csv，按反推帧率对齐到 1 s 网格。"""
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    cand_cols = [c for c in df.columns if c.strip().lower() == "motion"]
    if not cand_cols:
        return None
    mv = df[cand_cols[0]].to_numpy(dtype=float)
    if mv.size == 0 or duration_s <= 0:
        return None
    fps = mv.size / duration_s
    if not (1.0 <= fps <= 200.0):
        log("      [警告] 行为数据反推帧率 %.2f fps 不合理，可能与荧光记录时长不同步，C 面板仅供参考。" % fps)
    sec = np.floor(np.arange(mv.size) / fps).astype(int)
    out = np.zeros(sec[-1] + 1, float)
    np.maximum.at(out, sec, np.nan_to_num(mv))
    tt = np.arange(out.size) / fs
    return tt, out


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
class Job(object):
    def __init__(self):
        self.fluo = ""
        self.events = ""
        self.motion = ""
        self.start_min = 30.0
        self.end_min = 90.0
        self.thr = 3.0
        self.min_dur = 1.0
        self.mode = "start_end"      # 'single' | 'start_end'
        self.name = "Injection"
        self.outdir = ""
        self.time_unit = "auto"      # 'auto' | 'ms' | 's'


def run_pipeline(job, log=print):
    """执行分析 + 写文件 + 出图。返回参数字典。源文件永不写入。"""
    src_fluo = os.path.abspath(job.fluo)
    src_events = os.path.abspath(job.events) if job.events else ""
    if not os.path.isfile(src_fluo):
        raise IOError("找不到 Fluorescence.csv：%s" % src_fluo)
    if src_events and not os.path.isfile(src_events):
        log("      [警告] 找不到 Events.csv（%s），将新建只含 marker 的文件。" % src_events)
        src_events = ""

    src_dir = os.path.dirname(src_fluo)
    outdir = os.path.abspath(job.outdir.strip() or os.path.join(src_dir, "injection_marker"))
    if os.path.isfile(outdir):
        raise IOError("输出路径是一个已存在的文件，请指定目录：%s" % outdir)
    # 原始数据只读：任何输出文件都不得与源文件重合
    srcs = [src_fluo] + ([src_events] if src_events else [])
    for nm in ("Fluorescence.csv", "Events.csv", "injection_window.png"):
        dst = os.path.abspath(os.path.join(outdir, nm)).lower()
        if dst in [s.lower() for s in srcs]:
            raise RuntimeError("拒绝执行：输出文件 %s 会覆盖源文件。请换一个输出目录（原始数据只读）。" % dst)
    os.makedirs(outdir, exist_ok=True)

    log("[1/4] 读取数据 …")
    ts_raw, ch4 = read_fluorescence(src_fluo)
    t = to_seconds(ts_raw, getattr(job, "time_unit", "auto"))
    dt_ms = float(np.median(np.diff(t)))
    log("      采样点 %d，时长 %.2f h，采样间隔 %.0f ms，通道均值 %s" %
        (len(t), t[-1] / 3600.0, dt_ms * 1000.0, np.round(ch4.mean(axis=0), 2)))

    log("[2/4] 计算伪影指数 …")
    idx, hp, tu = compute_artifact_index(ch4, t)
    events = detect_events(idx, tu, job.thr, job.min_dur)
    cand = pick_candidate(events, job.start_min * 60.0, job.end_min * 60.0)
    if cand is None:
        raise RuntimeError("在 %.0f–%.0f min 内没有检测到超过阈值 %.1f 的事件，"
                           "可降低阈值或放宽时间窗后重试。" % (job.start_min, job.end_min, job.thr))
    log("      候选窗 %.2f–%.2f min（峰值 %.1f×基线 @ %.2f min，%.1f s，%d 个事件段）" %
        (cand[0] / 60.0, cand[1] / 60.0, cand[2], cand[3] / 60.0, cand[1] - cand[0], cand[4]))

    if job.mode == "single":
        mid = (cand[0] + cand[1]) / 2.0
        markers = [(mid, job.name)]
    else:
        markers = [(cand[0], job.name + "Start"), (cand[1], job.name + "End")]

    log("[3/4] 写 marker …")
    dst_fluo = os.path.join(outdir, "Fluorescence.csv")
    dst_ev = os.path.join(outdir, "Events.csv")
    hits = write_fluorescence(src_fluo, dst_fluo, markers,
                              getattr(job, "time_unit", "auto"), warn=log)
    if len(hits) != len(markers):
        log("      [警告] 只有 %d/%d 个 marker 落到采样点（时间戳可能有缺失）。" % (len(hits), len(markers)))
    for (t_mark, name), (_, t_act) in zip(markers, hits):
        if abs(t_act - t_mark) > 1.0:
            log("      [警告] marker %s 距最近采样点 %.2f s，已吸附。" % (name, abs(t_act - t_mark)))
    write_events(src_events, dst_ev, hits)
    log("      %s (%.1f MB)" % (dst_fluo, os.path.getsize(dst_fluo) / 1e6))
    log("      %s" % dst_ev)

    log("[4/4] 出图 …")
    motion = None
    if job.motion and os.path.isfile(job.motion):
        motion = load_motion(job.motion, t[-1], log=log)
    tag = "：%.2f–%.2f min 检出注射相关运动伪影" % (cand[0] / 60.0, cand[1] / 60.0)
    png = os.path.join(outdir, "injection_window.png")
    make_figure(png, tu, idx, hp, cand, motion=motion, title_tag=tag)
    log("      %s" % png)

    return dict(win=(cand[0], cand[1]), peak=cand[2], peak_t=cand[3],
                n_events=cand[4], markers=markers, hits=hits, outdir=outdir,
                data_mb=os.path.getsize(src_fluo) / 1e6)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("光纤记录 · 注射 marker 自动标注")
    root.geometry("880x620")
    root.minsize(760, 560)

    vars_ = {}
    row = [0]

    def add_path(label_text, key, default, is_dir=False):
        frm = ttk.Frame(root, padding=(10, 4, 10, 0))
        frm.pack(fill="x")
        ttk.Label(frm, text=label_text, width=18, anchor="w").pack(side="left")
        v = tk.StringVar(value=default)
        vars_[key] = v
        ent = ttk.Entry(frm, textvariable=v)
        ent.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def browse():
            if is_dir:
                p = filedialog.askdirectory()
            else:
                p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")])
            if p:
                v.set(p)

        ttk.Button(frm, text="浏览…", command=browse, width=8).pack(side="left")
        return v

    Z = r"Z:\Ruyi_RawData\Fiber\eCB\DrugIntake\NAcSh\WT\20240729-WT-NAcSh-AEA1.5L-2-AG1.5R-THC-dose\C19_10mpk_THC"
    add_path("Fluorescence.csv", "fluo", os.path.join(Z, "Fluorescence.csv"))
    add_path("Events.csv（可空）", "events", os.path.join(Z, "Events.csv"))
    add_path("行为数据（可选）", "motion", "")

    # 参数区
    p = ttk.Frame(root, padding=(10, 10, 10, 0))
    p.pack(fill="x")
    ttk.Label(p, text="搜索窗口").grid(row=0, column=0, sticky="w")
    v1 = tk.StringVar(value="30"); vars_["start"] = v1
    v2 = tk.StringVar(value="90"); vars_["end"] = v2
    ttk.Entry(p, textvariable=v1, width=7).grid(row=0, column=1, padx=(4, 2))
    ttk.Label(p, text="–").grid(row=0, column=2)
    ttk.Entry(p, textvariable=v2, width=7).grid(row=0, column=3, padx=(2, 10))
    ttk.Label(p, text="min").grid(row=0, column=4, sticky="w")

    ttk.Label(p, text="阈值").grid(row=0, column=5, padx=(16, 0), sticky="w")
    v3 = tk.StringVar(value="3.0"); vars_["thr"] = v3
    ttk.Entry(p, textvariable=v3, width=7).grid(row=0, column=6, padx=(4, 10))

    ttk.Label(p, text="最短时长").grid(row=0, column=7, sticky="w")
    v4 = tk.StringVar(value="1.0"); vars_["dur"] = v4
    ttk.Entry(p, textvariable=v4, width=7).grid(row=0, column=8, padx=(4, 4))
    ttk.Label(p, text="s").grid(row=0, column=9, sticky="w")

    p2 = ttk.Frame(root, padding=(10, 8, 10, 0))
    p2.pack(fill="x")
    ttk.Label(p2, text="marker 名").grid(row=0, column=0, sticky="w")
    v5 = tk.StringVar(value="Injection"); vars_["name"] = v5
    ttk.Entry(p2, textvariable=v5, width=16).grid(row=0, column=1, padx=(4, 16))
    v6 = tk.StringVar(value="start_end"); vars_["mode"] = v6
    ttk.Radiobutton(p2, text="起止两点（Start/End）", variable=v6,
                    value="start_end").grid(row=0, column=2, padx=(0, 12))
    ttk.Radiobutton(p2, text="单点（窗口中心）", variable=v6,
                    value="single").grid(row=0, column=3)

    # 输出目录
    ofrm = ttk.Frame(root, padding=(10, 8, 10, 0))
    ofrm.pack(fill="x")
    ttk.Label(ofrm, text="输出目录", width=18, anchor="w").pack(side="left")
    vo = tk.StringVar(value="")
    vars_["outdir"] = vo
    ttk.Entry(ofrm, textvariable=vo).pack(side="left", fill="x", expand=True, padx=(0, 6))
    ttk.Label(ofrm, text="（留空 = 源目录下 injection_marker\\）", font=(UI_FONT, 8)).pack(side="left")

    # 按钮
    btn = ttk.Frame(root, padding=(10, 10, 10, 4))
    btn.pack(fill="x")
    b_run = ttk.Button(btn, text="开始分析并写出", width=18)
    b_run.pack(side="left")
    b_quit = ttk.Button(btn, text="退出", width=8, command=root.destroy)
    b_quit.pack(side="left", padx=(8, 0))

    # 日志
    lf = ttk.Frame(root, padding=(10, 4, 10, 10))
    lf.pack(fill="both", expand=True)
    txt = tk.Text(lf, height=16, wrap="word", font=(MONO_FONT, 9))
    sb = ttk.Scrollbar(lf, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    # 工作线程只往队列里放消息，所有 Tk 操作都在主线程做
    q = queue.Queue()

    def log(msg):
        q.put(("log", str(msg)))

    def _done(res):
        b_run.configure(state="normal")
        log("")
        log("=== 完成 ===")
        log("注射候选窗：%.2f – %.2f min" % (res["win"][0] / 60.0, res["win"][1] / 60.0))
        log("峰值 %.1f×基线 @ %.2f min（%.1f s，%d 个事件段）" %
            (res["peak"], res["peak_t"] / 60.0, res["win"][1] - res["win"][0], res["n_events"]))
        log("marker：" + "，".join("%s @ %.2f min" % (n, tt / 60.0)
                                  for tt, n in res["markers"]))
        log("输出目录：" + res["outdir"])
        if messagebox.askyesno("完成", "注射候选窗 %.2f–%.2f min。\n打开输出目录？"
                               % (res["win"][0] / 60.0, res["win"][1] / 60.0)):
            try:
                open_in_file_manager(res["outdir"])
            except Exception:
                pass

    def _fail(err):
        b_run.configure(state="normal")
        msg, tb = err if isinstance(err, tuple) else (err, "")
        log("")
        log("[错误] " + str(msg))
        if tb:
            log(tb)
        messagebox.showerror("失败", str(msg))

    def drain():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "log":
                    txt.insert("end", payload + "\n")
                    txt.see("end")
                elif kind == "ok":
                    _done(payload)
                elif kind == "err":
                    _fail(payload)
        except queue.Empty:
            pass
        root.after(120, drain)

    def work(job):
        try:
            res = run_pipeline(job, log=log)
            q.put(("ok", res))
        except Exception as exc:            # 立即绑定，避免延迟求值失效
            q.put(("err", (exc, traceback.format_exc())))

    def do_run():
        job = Job()
        job.fluo = vars_["fluo"].get().strip().strip('"')
        job.events = vars_["events"].get().strip().strip('"')
        job.motion = vars_["motion"].get().strip().strip('"')
        job.outdir = vars_["outdir"].get().strip().strip('"')
        try:
            job.start_min = float(vars_["start"].get())
            job.end_min = float(vars_["end"].get())
            job.thr = float(vars_["thr"].get())
            job.min_dur = float(vars_["dur"].get())
        except ValueError:
            messagebox.showerror("参数错误", "时间窗口 / 阈值 / 最短时长必须是数字。")
            return
        if job.start_min >= job.end_min:
            messagebox.showerror("参数错误", "搜索窗口起点必须小于终点。")
            return
        job.name = vars_["name"].get().strip() or "Injection"
        job.mode = vars_["mode"].get()

        src_path = os.path.abspath(job.fluo) if job.fluo else ""
        if not src_path or not os.path.isfile(src_path):
            messagebox.showerror("找不到文件", "请指定有效的 Fluorescence.csv。")
            return
        src_dir = os.path.dirname(src_path)
        outdir = os.path.abspath(job.outdir or os.path.join(src_dir, "injection_marker"))
        if outdir.lower() == src_dir.lower():
            messagebox.showerror("输出目录无效",
                                 "输出目录不能是源文件所在目录（会覆盖原始数据）。\n"
                                 "请留空使用默认的 injection_marker 子目录。")
            return
        if os.path.isdir(outdir) and any(
                os.path.isfile(os.path.join(outdir, f))
                for f in ("Fluorescence.csv", "Events.csv", "injection_window.png")):
            if not messagebox.askyesno("确认覆盖", "%s\n\n该目录下已有结果文件，是否覆盖？" % outdir):
                return

        b_run.configure(state="disabled")
        txt.delete("1.0", "end")
        threading.Thread(target=work, args=(job,), daemon=True).start()

    b_run.configure(command=do_run)
    log("准备就绪。原始文件只读，结果写入「源目录\\injection_marker\\」。")
    log("分析：四通道去 5-min 漂移 → 2-s 高频能量 → 归一化平均 → 阈值检测 → 时间窗内最强事件簇。")
    root.after(120, drain)
    root.mainloop()


def main():
    try:                       # 控制台中文不乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="光纤记录注射 marker 自动标注")
    ap.add_argument("--nogui", action="store_true", help="不启动界面，直接跑一次")
    ap.add_argument("--fluo", default="")
    ap.add_argument("--events", default="")
    ap.add_argument("--motion", default="")
    ap.add_argument("--outdir", default="")
    ap.add_argument("--start", type=float, default=30.0)
    ap.add_argument("--end", type=float, default=90.0)
    ap.add_argument("--thr", type=float, default=3.0)
    ap.add_argument("--dur", type=float, default=1.0)
    ap.add_argument("--name", default="Injection")
    ap.add_argument("--mode", default="start_end", choices=["start_end", "single"])
    ap.add_argument("--time-unit", default="auto", choices=["auto", "ms", "s"],
                    help="时间戳单位（默认自动判断）")
    a = ap.parse_args()

    if a.nogui or a.fluo:
        job = Job()
        job.fluo, job.events, job.motion, job.outdir = a.fluo, a.events, a.motion, a.outdir
        job.start_min, job.end_min = a.start, a.end
        job.thr, job.min_dur = a.thr, a.dur
        job.name, job.mode = a.name, a.mode
        job.time_unit = a.time_unit
        if not job.fluo:
            ap.error("--nogui 需要 --fluo")
        if job.start_min >= job.end_min:
            ap.error("--start 必须小于 --end（当前 %.2f >= %.2f）" % (job.start_min, job.end_min))
        run_pipeline(job, log=lambda m: print(m, flush=True))
    else:
        launch_gui()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except (RuntimeError, ValueError, OSError) as _exc:
        # Every one of these is a deliberate refusal (bad range, no candidate,
        # output would overwrite the source), so it gets one clean line instead
        # of a traceback, and never a modal box in a scripted run.
        print("错误：%s" % _exc, file=sys.stderr)
        if _is_gui_run():
            try:
                from tkinter import messagebox as _mb_err
                _mb_err.showerror("InjectionFinder", str(_exc))
            except Exception:
                pass
        sys.exit(1)
    except Exception:               # 未捕获异常留痕，避免窗口一闪而过没有线索
        _detail = traceback.format_exc()
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "injection_finder_crash.log"), "w", encoding="utf-8") as _fh:
                _fh.write(_detail)
        except Exception:
            pass
        print(_detail, file=sys.stderr)
        if _is_gui_run():
            try:
                from tkinter import messagebox as _mb2
                _mb2.showerror("InjectionFinder 运行出错",
                               "详细原因已写入 injection_finder_crash.log\n\n" + _detail[-1500:])
            except Exception:
                pass
        sys.exit(1)
