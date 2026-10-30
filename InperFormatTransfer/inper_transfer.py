"""把 Inper（瑞沃德）data.db 转换成 InperView 导出格式。

产出与 InperView「导出荧光数据」完全同构的 4 个文件，直接落在 session 目录：

    Fluorescence.csv            对齐后的 10 Hz 数据（410/470 合成一行）
    Fluorescence-unaligned.csv  20 fps 原始帧（每帧一行，Lights 列标记波长）
    Events.csv                  手动/外部打标时刻（State=1）
    Outputs.csv                 数字输出表头（Inper db 无输出通道时为空表）

用法：
    python inper_transfer.py <session_dir>
    python inper_transfer.py <session_dir> -o <out_dir>
    python inper_transfer.py --batch <root_dir>

默认写进 session 目录，已存在的输出文件默认不覆盖（加 --overwrite）。
原始 data.db 全程只读打开。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

from inper_db import Recording, load_recording

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

FLUORESCENCE = "Fluorescence.csv"
UNALIGNED = "Fluorescence-unaligned.csv"
EVENTS = "Events.csv"
OUTPUTS = "Outputs.csv"
ALL_OUTPUTS = (FLUORESCENCE, UNALIGNED, EVENTS, OUTPUTS)

DEFAULT_EVENT_COLORS = [
    "#FFFFFF00", "#FFC00000", "#FF0099FF", "#FFFF0000",
    "#FF00B0F0", "#FF000000", "#FF0099FF",
]
# InperView 的 4 个数字输入通道颜色固定
INPUT_COLORS = ("#FFFF0000", "#FF00B0F0", "#FF000000", "#FF0099FF")
INPUT_MARKS = ("Input1", "Input2", "Input3", "Input4")


# --------------------------------------------------------------------------- #
# 读取（网络盘先大块复制到本地）
# --------------------------------------------------------------------------- #

_COPY_CHUNK = 4 * 1024 * 1024  # 网络盘按大块顺序复制，比 sqlite 逐页读快 10 倍以上


def _is_remote_path(path: Path) -> bool:
    """db 是否在网络盘上（UNC 路径或 Windows 网络驱动器）。"""
    text = str(path)
    if os.name != "nt":
        return text.startswith("//")
    if text.startswith("\\\\"):
        return True
    drive = os.path.splitdrive(text)[0]
    if not drive:
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
    except Exception:  # noqa: BLE001 - 判断失败就当本地盘，直接读
        return False


def _stage_local(db_path: Path) -> tuple[Path, Path]:
    """把 db 顺序复制到本地临时目录，返回 (临时 db 路径, 临时目录)。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="inper_stage_"))
    dst = tmp_dir / "data.db"
    try:
        with db_path.open("rb", buffering=0) as src, dst.open("wb", buffering=0) as out:
            while True:
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
    except OSError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return dst, tmp_dir


# --------------------------------------------------------------------------- #
# 对齐
# --------------------------------------------------------------------------- #

def build_aligned(rec: Recording):
    """把交替帧合并成采样点行，逐行 yield (t_ms, {wavelength: values})。

    以出现次数最多的波长为主轴（通常 410），每个其它波长的帧与最近的
    主轴帧一对一配对，允许的最大偏差为半个采样周期。
    """
    frames = rec.frames
    waves = rec.wavelengths  # 已缓存，避免每次重扫 100 万帧
    pair_window_ns = 1e9 / rec.sampling * 0.6

    by_wl = {wl: [] for wl in waves}
    for f in frames:  # 一次遍历完成分组（原实现每个波长扫一遍全表）
        by_wl[f.wavelength].append(f)
    primary = max(waves, key=lambda w: (len(by_wl[w]), -w))
    others = [w for w in waves if w != primary]

    cursor = {w: 0 for w in others}
    for pf in by_wl[primary]:
        row = {primary: pf.values}
        for w in others:
            seq = by_wl[w]
            j = cursor[w]
            while j < len(seq) and seq[j].t_ns < pf.t_ns - pair_window_ns:
                j += 1
            if j < len(seq) and abs(seq[j].t_ns - pf.t_ns) <= pair_window_ns:
                row[w] = seq[j].values
                j += 1
            cursor[w] = j
        yield pf.t_ns, row


# --------------------------------------------------------------------------- #
# 时间零点 & 实测帧率
# --------------------------------------------------------------------------- #

def anchor_to_primary(rec: Recording) -> int:
    """把对齐表的时间零点对齐到主轴光源的第一帧，使输出首行 TimeStamp 恒为 0.000。

    InperView 自己的导出永远从 0.000 开始。旧版 db 的 camera_clock 是两个光源共用
    的绝对时钟；若另一个光源有一帧早于主轴首帧，t0 取全体最小值就会让整条时间轴
    整体后移半个采样周期（实测 47.62 ms），首行变成 47.620 而不是 0.000。

    这里只改「行」的零点（rec.row_t0_ns），不删任何帧：那条早到的他光源帧仍参与
    Fluorescence-unaligned.csv 和配对，因此不丢数据。

    返回该偏移量（ns），0 表示无需调整。
    """
    frames = rec.frames
    if not frames:
        return 0
    counts = Counter(f.wavelength for f in frames)
    primary = max(rec.wavelengths, key=lambda w: (counts[w], -w))
    first = next(f.t_ns for f in frames if f.wavelength == primary)
    rec._row_t0_ns = first
    return first - rec.t0_ns


def measured_fps(rec: Recording) -> float:
    """由实际帧间隔算相机帧率，与标称值差 >2% 时返回实测值。

    Inper 的 frame_rate/Sampling 是设定值，实际采集速率可能偏离（2023 那批实测
    每光源 95.24 ms ≈ 10.5 Hz，相机 21.0 fps，与标称 20 差 5%）。meta 的 Fps
    要与真实时间轴一致，否则下游按 Fps 推算时长会整体偏 ~5%。
    """
    ts = [f.t_ns for f in rec.frames]
    diffs = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    if len(diffs) < 10:
        return rec.fps
    camera_period_ns = diffs[len(diffs) // 2]  # 交替照明下相邻两帧即一个相机周期
    fps = 1e9 / camera_period_ns
    nominal = rec.fps
    if nominal > 0 and abs(fps - nominal) / nominal <= 0.02:
        return nominal
    return round(fps, 1)


# --------------------------------------------------------------------------- #
# meta 行
# --------------------------------------------------------------------------- #

def _wave_from_label(text: str) -> int | None:
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    return int(digits) if digits else None


def build_meta(rec: Recording) -> str:
    """按 InperView 的字段顺序拼第一行 meta（分隔符是 ';' 而非 ','）。"""
    n_ch = max(rec.n_channels, 1)

    # Light
    enabled = {410: False, 470: False, 560: False}
    power = {410: 9.0, 470: 20.0, 560: 4.0}
    for item in rec.light_mode:
        wl = _wave_from_label(item.get("WaveType", ""))
        if wl in enabled:
            enabled[wl] = bool(item.get("IsChecked"))
            try:
                power[wl] = float(item.get("LightPower"))
            except (TypeError, ValueError):
                pass
    light = (
        '{"Light":{'
        f'"Led410Enable":{str(enabled[410]).lower()};'
        f'"Led470Enable":{str(enabled[470]).lower()};'
        f'"Led560Enable":{str(enabled[560]).lower()};'
        f'"Led410Value":{repr(power[410])};'
        f'"Led470Value":{repr(power[470])};'
        f'"Led560Value":{repr(power[560])}'
        "}"
    )

    # Excitation（连续记录；字段值按 InperView 惯例填 20）
    discontinuous = not bool(rec.record_mode.get("IsContinuous", True))
    excitation = (
        ';"Excitation":{"mode":1;'
        f'"discontinuous":{str(discontinuous).lower()};'
        '"interval_time":20;"continuous_time":20}'
    )

    # Channels：db 只存一个 ROI 框，Roi1/Roi2 都填它（InperView 的第二 ROI 无对应来源）
    chans = []
    for i in range(n_ch):
        cfg = rec.channels[i] if i < len(rec.channels) else {}
        try:
            left = float(cfg.get("Left"))
            top = float(cfg.get("Top"))
            roi = float(cfg.get("ROI"))
        except (TypeError, ValueError):
            left = top = roi = 0.0
        box = f"{left!r};{top!r};{roi!r};{roi!r}"
        chans.append(f'{{"Name":"CH{i + 1}";"ChanelColor":"#00000000";'
                     f'"Roi1":"{box}";"Roi2":"{box}"}}')
    channel_ids = ';'.join(f'"CH{i + 1}"' for i in range(n_ch))
    channels = ';"Channels":[' + ";".join(chans) + "]"

    # AllMarking：手动标记 + 数字输入
    names = []
    for name in list(rec.event_names) + [n for n, _ in rec.markers]:
        if name and name not in names:
            names.append(name)
    if not names:
        names = ["i.p."]
    marks = []
    for idx, name in enumerate(names):
        color = DEFAULT_EVENT_COLORS[idx % len(DEFAULT_EVENT_COLORS)]
        marks.append(
            f'{{"id":0;"type":1;"opened":true;"name":"{name}";"Levels":0;'
            f'"color":"{color}";"key":0;"threshold":0.0;"ID":"0";"all":true;'
            f'"channels":[{channel_ids}];"enable":true}}'
        )
    for idx, name in enumerate(INPUT_MARKS):
        color = INPUT_COLORS[idx]
        marks.append(
            f'{{"id":0;"type":2;"opened":true;"name":"{name}";"Levels":0;'
            f'"color":"{color}";"key":0;"threshold":0.0;"ID":"0";"all":true;'
            f'"channels":[{channel_ids}];"enable":true}}'
        )
    marking = ';"AllMarking":[' + ";".join(marks) + "]"

    return light + excitation + channels + marking + f';"Fps":{repr(measured_fps(rec))}}}'


# --------------------------------------------------------------------------- #
# 写文件
# --------------------------------------------------------------------------- #

def _column_names(rec: Recording) -> list[str]:
    n_ch = max(rec.n_channels, 1)
    waves = rec.wavelengths or [410]
    return [f"CH{ch}-{wl}" for ch in range(1, n_ch + 1) for wl in waves]


def write_fluorescence(rec: Recording, out_path: Path, log=None) -> dict:
    waves = rec.wavelengths or [410]
    n_ch = max(rec.n_channels, 1)
    t0 = rec.row_t0_ns
    header = "TimeStamp,Events," + ",".join(_column_names(rec)) + ","
    count = 0
    buf: list[str] = []
    with out_path.open("w", encoding="utf-8", newline="", buffering=1 << 20) as fh:
        fh.write(build_meta(rec) + "\n")
        fh.write(header + "\n")
        for t_ns, row in build_aligned(rec):
            # 每个波长只取一次 values，避免在通道循环里重复 row.get
            cols = [row.get(wl) for wl in waves]
            cells = [f"{(t_ns - t0) / 1e6:.3f}", ""]
            for ch in range(n_ch):
                for vals in cols:
                    if vals is not None and ch < len(vals):
                        v = vals[ch]
                        cells.append(f"{v:.3f}" if v == v else "")
                    else:
                        cells.append("")
            buf.append(",".join(cells) + ",\n")
            count += 1
            if len(buf) >= 8192:  # 批量落盘，减少 write 调用
                fh.write("".join(buf))
                buf.clear()
            if log and count % 50000 == 0:
                log(f"    {FLUORESCENCE}: {count:,} rows")
        if buf:
            fh.write("".join(buf))
    return {"rows": count}


def write_unaligned(rec: Recording, out_path: Path, log=None) -> dict:
    n_ch = max(rec.n_channels, 1)
    t0 = rec.t0_ns
    header = "TimeStamp,Lights," + ",".join(f"Channel{i + 1}" for i in range(n_ch))
    wl_str = {wl: str(wl) for wl in (rec.wavelengths or [410])}  # 波长只有 2~3 个，预建字符串
    count = 0
    buf: list[str] = []
    with out_path.open("w", encoding="utf-8", newline="", buffering=1 << 20) as fh:
        fh.write(header + "\n")
        for fr in rec.frames:
            vals = fr.values
            cells = [f"{(fr.t_ns - t0) / 1e6:.3f}", wl_str.get(fr.wavelength, str(fr.wavelength))]
            if len(vals) == n_ch:  # 常见情形：省掉切片分配
                cells += [f"{v:.3f}" if v == v else "" for v in vals]
            else:
                cells += [f"{v:.3f}" if v == v else "" for v in vals[:n_ch]]
                if len(vals) < n_ch:
                    cells += [""] * (n_ch - len(vals))
            buf.append(",".join(cells) + "\n")
            count += 1
            if len(buf) >= 16384:
                fh.write("".join(buf))
                buf.clear()
            if log and count % 100000 == 0:
                log(f"    {UNALIGNED}: {count:,} rows")
        if buf:
            fh.write("".join(buf))
    return {"rows": count}


def write_events(rec: Recording, out_path: Path) -> dict:
    t0 = rec.row_t0_ns
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write("TimeStamp,Name,State\n")
        for name, t_ns in rec.markers:
            fh.write(f"{(t_ns - t0) / 1e6:.3f},{name},1\n")
    return {"rows": len(rec.markers)}


def write_outputs(out_path: Path) -> dict:
    out_path.write_text("TimeStamp,Output1,Output2,Output3,Output4\n", encoding="utf-8")
    return {"rows": 0}


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def validate(rec: Recording, stats: dict, out_dir: Path) -> list[str]:
    problems = []
    aligned = stats.get("aligned", {}).get("rows", 0)
    unaligned = stats.get("unaligned", {}).get("rows", 0)

    counts = Counter(f.wavelength for f in rec.frames)  # 一次遍历代替两次全表扫描

    primary = max(rec.wavelengths, key=lambda w: (counts[w], -w))
    expected = counts[primary]
    if aligned != expected:
        problems.append(f"aligned rows {aligned} != primary frames {expected}")
    if stats.get("unaligned") and stats["unaligned"].get("rows") != len(rec.frames):
        problems.append(f"unaligned rows {stats['unaligned']['rows']} != frames {len(rec.frames)}")

    # 时间单调 + 数值抽查
    path = out_dir / FLUORESCENCE
    with path.open("r", encoding="utf-8") as fh:
        fh.readline()  # meta
        header = fh.readline().strip().split(",")
        cols = [i for i, name in enumerate(header) if name.startswith("CH")]
        last_t = -1.0
        checked = 0
        for line in fh:
            parts = line.rstrip("\n").split(",")
            try:
                t = float(parts[0])
            except (ValueError, IndexError):
                problems.append("non-numeric TimeStamp")
                break
            if t <= last_t:
                problems.append(f"TimeStamp not increasing at {t}")
                break
            last_t = t
            checked += 1
            if checked >= 2000:
                break
    return problems


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def convert_session(session_dir: Path, out_dir: Path | None = None,
                    overwrite: bool = False, skip_unaligned: bool = False,
                    log=None, stage: bool = True) -> dict:
    emit = log or (lambda _msg: None)
    session_dir = Path(session_dir)
    db_path = session_dir / "data.db"
    if not db_path.is_file():
        raise FileNotFoundError(f"data.db not found in {session_dir}")

    out_dir = Path(out_dir) if out_dir else session_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [FLUORESCENCE, EVENTS, OUTPUTS] + ([] if skip_unaligned else [UNALIGNED])
    existing = [n for n in targets if (out_dir / n).exists()]
    if existing and not overwrite:
        raise FileExistsError(
            f"output already exists in {out_dir}: {', '.join(existing)} "
            f"(enable Replace existing files)"
        )

    emit(f"reading {db_path} ...")
    staged_dir = None
    load_path = db_path
    if stage and _is_remote_path(db_path):
        # sqlite 逐页读网络盘极慢（实测 65 MB / 92 s）；整块顺序复制到本地只要几秒
        emit("copying data.db to local temp (network drive detected) ...")
        try:
            load_path, staged_dir = _stage_local(db_path)
        except OSError as exc:
            emit(f"staging skipped ({exc}); reading in place")
            load_path, staged_dir = db_path, None
    try:
        rec = load_recording(load_path)
    finally:
        if staged_dir is not None:
            shutil.rmtree(staged_dir, ignore_errors=True)
    rec.db_path = db_path  # 临时副本已删除，记录原始位置
    if not rec.frames:
        raise ValueError(f"no fluorescence frames parsed from {db_path}")
    dropped = anchor_to_primary(rec)
    if dropped:
        emit(f"time zero set to the primary light's first frame (+{dropped / 1e6:.3f} ms shift)")
    emit(f"parsed {len(rec.frames):,} frames | {rec.duration_s() / 3600:.2f} h | "
         f"channels={rec.n_channels} | wavelengths={rec.wavelengths}")

    stats = {"aligned": {}, "unaligned": {}, "events": {}, "staged": staged_dir is not None}
    emit(f"writing {FLUORESCENCE} ...")
    stats["aligned"] = write_fluorescence(rec, out_dir / FLUORESCENCE, log=emit)
    if not skip_unaligned:
        emit(f"writing {UNALIGNED} ...")
        stats["unaligned"] = write_unaligned(rec, out_dir / UNALIGNED, log=emit)
    emit(f"writing {EVENTS} and {OUTPUTS} ...")
    stats["events"] = write_events(rec, out_dir / EVENTS)
    write_outputs(out_dir / OUTPUTS)

    emit("validating output ...")
    stats["problems"] = validate(rec, stats, out_dir)
    stats["rec"] = {
        "frames": len(rec.frames),
        "wavelengths": rec.wavelengths,
        "channels": rec.n_channels,
        "fps": measured_fps(rec),
        "duration_h": rec.duration_s() / 3600,
        "markers": len(rec.markers),
    }
    stats["out_dir"] = out_dir
    return stats


def _print_report(session: Path, stats: dict) -> None:
    info = stats["rec"]
    print(f"[ok] {session}")
    print(f"     frames={info['frames']:,}  wavelengths={info['wavelengths']}  "
          f"channels={info['channels']}  fps={info['fps']}  duration={info['duration_h']:.2f} h")
    print(f"     aligned={stats['aligned']['rows']:,}  "
          f"unaligned={stats['unaligned'].get('rows', 0):,}  markers={stats['events']['rows']}")
    if stats["problems"]:
        for problem in stats["problems"]:
            print(f"     [WARN] {problem}")
    else:
        print("     [ok] columns, timestamps and row counts validated")


def _iter_sessions(root: Path):
    seen = set()
    for db in sorted(root.rglob("data.db")):
        parent = db.parent.absolute()
        if parent in seen:
            continue
        seen.add(parent)
        yield parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert Inper data.db into InperView Fluorescence.csv / Events.csv layout."
    )
    parser.add_argument("session", nargs="?", help="session folder containing data.db")
    parser.add_argument("--batch", metavar="ROOT", help="recursively convert every data.db under ROOT")
    parser.add_argument("-o", "--output-dir", help="write outputs here instead of the session folder")
    parser.add_argument("--overwrite", action="store_true", help="replace existing output files")
    parser.add_argument("--skip-unaligned", action="store_true",
                        help="do not write Fluorescence-unaligned.csv (saves ~half the disk)")
    parser.add_argument("--in-place", action="store_true",
                        help="read network data.db in place instead of staging it to local temp")
    args = parser.parse_args(argv)

    if bool(args.session) == bool(args.batch):
        parser.error("give exactly one of <session> or --batch <root>")

    if args.session:
        sessions = [Path(args.session).absolute()]
    else:
        sessions = list(_iter_sessions(Path(args.batch).absolute()))
    failed = 0
    for session in sessions:
        if not session.is_dir():
            print(f"[skip] not a folder: {session}")
            failed += 1
            continue
        try:
            stats = convert_session(
                session,
                out_dir=Path(args.output_dir) if args.output_dir else None,
                overwrite=args.overwrite,
                skip_unaligned=args.skip_unaligned,
                stage=not args.in_place,
            )
            _print_report(session, stats)
        except FileExistsError as exc:
            print(f"[skip] {session}: {exc}")
        except Exception as exc:  # noqa: BLE001 - CLI 顶层需要把失败点讲清楚
            print(f"[fail] {session}: {type(exc).__name__}: {exc}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
