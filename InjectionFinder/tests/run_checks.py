# -*- coding: utf-8 -*-
"""边界自检：源文件保真（编码 / 换行 / 坏行）、CLI 校验、无候选、输出=源目录。

全部用合成数据，不依赖真实记录。产物写在 tests/_tmp/。
"""
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GUI = os.path.join(ROOT, "injection_marker_gui.py")
TMP = os.path.join(HERE, "_tmp")
PY = sys.executable


def _rows(n=12000, fs=10.0, burst_s=(500.0, 540.0)):
    """合成 4 通道：平滑基线 + 基线噪声 + 指定区间的高频爆发。"""
    rng = np.random.default_rng(0)
    ts = np.arange(n) / fs
    base = np.sin(ts / 300.0)
    noise = rng.normal(0, 0.01, n)
    m = (ts >= burst_s[0]) & (ts <= burst_s[1])
    noise[m] += rng.normal(0, 0.8, int(m.sum()))
    ch = np.column_stack([base + noise, 2 * base + noise,
                          1.5 * base + noise * 0.9, 3 * base + noise * 0.8])
    return ts, ch


def _write(path, ts, ch, meta, eol="\n", enc="utf-8"):
    lines = [meta, "TimeStamp,Events,CH1-410,CH1-470,CH2-410,CH2-470,"]
    for i in range(len(ts)):
        lines.append("%.3f,,%.4f,%.4f,%.4f,%.4f," %
                     (ts[i] * 1000.0, ch[i, 0], ch[i, 1], ch[i, 2], ch[i, 3]))
    with open(path, "wb") as f:
        f.write(eol.join(lines).encode(enc) + eol.encode(enc))


def run(name, args, expect_ok=True):
    p = subprocess.run([PY, GUI, "--nogui"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    ok = p.returncode == 0
    tag = "OK  " if (ok == expect_ok) else "FAIL"
    print("[%s] %s  (exit=%d)" % (tag, name, p.returncode))
    if not ok:
        print("      ", (p.stderr or "").strip().splitlines()[-1:])
    return p


def main():
    os.makedirs(TMP, exist_ok=True)
    ts, ch = _rows()
    meta = "Led410Enable:true, Fps:20.0, 备注:测试"

    for tag, eol, enc in (("lf_utf8", "\n", "utf-8"),
                          ("crlf_utf8", "\r\n", "utf-8"),
                          ("crlf_gbk", "\r\n", "gb18030")):
        d = os.path.join(TMP, tag)
        os.makedirs(d, exist_ok=True)
        src = os.path.join(d, "Fluorescence.csv")
        _write(src, ts, ch, meta, eol=eol, enc=enc)
        before = open(src, "rb").read()
        p = run(tag, ["--fluo", src, "--start", "5", "--end", "15",
                      "--outdir", os.path.join(d, "out")])
        if p.returncode != 0:
            continue
        after = open(os.path.join(d, "out", "Fluorescence.csv"), "rb").read()
        src_l = before.splitlines(keepends=True)
        out_l = after.splitlines(keepends=True)
        diff = [i for i, (a, b) in enumerate(zip(src_l, out_l)) if a != b]
        print("      行数 %d -> %d，差异行 %d 处，元数据+表头字节相同 %s" %
              (len(src_l), len(out_l), len(diff),
               src_l[0] == out_l[0] and src_l[1] == out_l[1]))
        print("      CRLF 数 源 %d / 出 %d；marker 行 %s" %
              (before.count(b"\r\n"), after.count(b"\r\n"),
               out_l[diff[0]].decode("utf-8", "replace").strip() if diff else "无"))
        ev = open(os.path.join(d, "out", "Events.csv"), encoding="utf-8").read().splitlines()
        print("      Events.csv: " + " | ".join(ev[1:]))

    # 数据中插入首字段非数字的坏行，检查两遍扫描是否仍然对齐
    d = os.path.join(TMP, "badrow")
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, "Fluorescence.csv")
    _write(src, ts, ch, meta)
    with open(src, "r", encoding="utf-8") as f:
        lines = f.read().split("\n")
    lines.insert(1600, "abc,,,1,2,3,")
    with open(src, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines))
    p = run("badrow", ["--fluo", src, "--start", "5", "--end", "15",
                       "--outdir", os.path.join(d, "out")])
    if p.returncode == 0:
        out = open(os.path.join(d, "out", "Fluorescence.csv"), encoding="utf-8",
                   errors="replace").read().split("\n")
        mrow = [l for l in out if "Injection" in l]
        print("      marker 行: " + (mrow[0].strip() if mrow else "缺失"))

    small = os.path.join(TMP, "lf_utf8", "Fluorescence.csv")
    run("bad_range  反向区间应报错", ["--fluo", small, "--start", "15", "--end", "5"],
        expect_ok=False)
    run("no_cand    窗口内无事件应报错", ["--fluo", small, "--start", "100", "--end", "110"],
        expect_ok=False)
    run("out=src    输出=源目录应拒绝", ["--fluo", small, "--start", "5", "--end", "15",
                                    "--outdir", os.path.join(TMP, "lf_utf8")],
        expect_ok=False)


if __name__ == "__main__":
    main()
