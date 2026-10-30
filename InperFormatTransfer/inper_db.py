"""Inper（瑞沃德 RWD 采集软件）光纤记录 data.db 解析层。

单一职责：把 data.db 读成 Python 结构，不做任何写盘。
只依赖标准库（sqlite3 / base64 / struct / json）。

支持 Inper（瑞沃德）两种 data.db schema，自动判别：

新版（2024-08 ~ 2025 各批次实测一致）
  * AllChannelRecord<n> 表按【光源】分表：n 只是序号，真正的波长看 Type 列
    （ColumnDesc: 0=410nm, 1=470nm, 2=561nm）。
  * 每行是一条相机帧记录，Value = "<ch>,<b64> <ch>,<b64> ..."，
    每个 b64 解出 8 字节 little-endian float64 = 该通道（ROI）的灰度均值。
  * CameraTime 单位纳秒，单调递增；410/470 交替照明，间隔 ≈ 1000/采样率 ms。
  * Marker 表保存手动/外部打标时刻（CameraTime，纳秒）。
  * 配置存在 Config 表（PropertyName/JsonText）。

旧版（≈2023 及更早，如 2023-12-17 这批）
  * 无 AllChannelRecord、无 Config；按波长直接建表："sig-410nm" / "sig-470nm"。
  * 列：sn, channel_1..channel_9, background, camera_clock, timestamp。
    值已是 REAL 明文灰度（不经过 base64）。
  * 时间列为 camera_clock（纳秒，绝对时钟，两个光源共用一个时钟）。
  * 配置存在 experiment_settings 表（section/content 键值对），
    含 channel_count / frame_rate / light{i}_wave_length / light{i}_power 等。
  * 另有 BLCPolynomialFittedsig-<wl>、MCorrected 等软件自算的校正/运动校正表，
    本模块不读（只取 sig-<wl> 原始信号）。
"""

from __future__ import annotations

import binascii
import json
import re
import sqlite3
import struct
from dataclasses import dataclass, field
from operator import itemgetter
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

# Type 列 -> 波长(nm)；与 ColumnDesc.Desc 一致
WAVELENGTH_BY_TYPE = {0: 410, 1: 470, 2: 560}
TYPE_BY_WAVELENGTH = {v: k for k, v in WAVELENGTH_BY_TYPE.items()}
TYPE_INPUT = -1  # 外部 Input 数字信号，不是荧光通道

# 模块级绑定：decode_values 每帧要调用两次，避免逐次查属性
_A2B = binascii.a2b_base64
_UNPACK_D = struct.unpack
_NAN = float("nan")


class Frame(NamedTuple):
    """一帧相机记录：时间(ns)、波长、各通道灰度值（按通道号索引）。"""

    t_ns: int
    wavelength: int
    values: tuple  # tuple[float, ...]，下标即通道号


@dataclass
class Recording:
    db_path: Path
    frames: list = field(default_factory=list)
    channels: list = field(default_factory=list)   # Config.CameraSignalSettings.CameraChannels
    sampling: float = 10.0                          # 每光源采样率(Hz)
    light_mode: list = field(default_factory=list)  # Config.CameraSignalSettings.LightMode
    record_mode: dict = field(default_factory=dict)
    markers: list = field(default_factory=list)     # [(name, t_ns)]
    event_names: list = field(default_factory=list)
    # 派生值缓存：load_recording() 填好后，下面几个属性都是 O(1)，
    # 避免每次访问都重新扫一遍上百万帧。
    _n_channels: int = field(default=0, repr=False)
    _wavelengths: tuple = field(default=(), repr=False)
    _t0_ns: int = field(default=-1, repr=False)
    _t_end_ns: int = field(default=-1, repr=False)
    _row_t0_ns: int = field(default=-1, repr=False)  # 对齐表(行)专用零点，见 inper_transfer.anchor_to_primary

    # ---- 派生属性 ----

    @property
    def n_channels(self) -> int:
        if not self._n_channels:
            self._n_channels = max(len(self.channels),
                                   max((len(f.values) for f in self.frames), default=0))
        return self._n_channels

    @property
    def wavelengths(self) -> list:
        if not self._wavelengths:
            self._wavelengths = tuple(sorted({f.wavelength for f in self.frames}))
        return list(self._wavelengths)

    @property
    def fps(self) -> float:
        """相机帧率 = 每光源采样率 × 光源数。"""
        return self.sampling * max(len(self.wavelengths), 1)

    @property
    def t0_ns(self) -> int:
        if self._t0_ns < 0:
            self._t0_ns = min((f.t_ns for f in self.frames), default=0)
        return self._t0_ns

    @property
    def t_end_ns(self) -> int:
        if self._t_end_ns < 0:
            self._t_end_ns = max((f.t_ns for f in self.frames), default=0)
        return self._t_end_ns

    @property
    def row_t0_ns(self) -> int:
        """对齐表(及 Events.csv)的时间零点；未显式设置时等于全体首帧。"""
        return self._row_t0_ns if self._row_t0_ns >= 0 else self.t0_ns

    def duration_s(self) -> float:
        if len(self.frames) < 2:
            return 0.0
        return (self.t_end_ns - self.t0_ns) / 1e9


def decode_value(text: str) -> dict:
    """解析 Value 字段：'1,<b64> 0,<b64>' -> {1: v1, 0: v0}。"""
    out = {}
    if not text:
        return out
    for part in text.split(" "):
        if not part:
            continue
        key, sep, b64 = part.partition(",")
        if not sep or not b64:
            continue
        try:
            out[int(key)] = struct.unpack("<d", binascii.a2b_base64(b64))[0]
        except (ValueError, struct.error):
            continue
    return out


def decode_values(text: str) -> tuple:
    """解析 Value 字段，直接返回按通道号索引的 tuple（缺失通道填 NaN）。

    '1,<b64> 0,<b64>' -> (v0, v1)。与 decode_value 不同，这里一次性建好
    定长 tuple，省掉调用方再 max()+生成器拼一遍的开销。

    定长快路径：Inper 绝大多数行都是 "k,<12 字符 b64> k,<12 字符 b64>"
    （总长 29），直接按位解码，省掉 split/partition/int/tuple 的开销（约快 2 倍）。
    键序或长度不符时回退到下面的通用解析。
    """
    if len(text) == 29 and text[1] == "," and text[14] == " " and text[16] == ",":
        k1 = text[0]
        k2 = text[15]
        try:
            v1 = _UNPACK_D("<d", _A2B(text[2:14]))[0]
            v2 = _UNPACK_D("<d", _A2B(text[17:29]))[0]
        except (ValueError, struct.error):
            pass
        else:
            if k1 == "1" and k2 == "0":
                return (v2, v1)
            if k1 == "0" and k2 == "1":
                return (v1, v2)

    if not text:
        return ()
    values: list = []
    for part in text.split(" "):
        if not part:
            continue
        key, sep, b64 = part.partition(",")
        if not sep or not b64:
            continue
        try:
            index = int(key)
            value = _UNPACK_D("<d", _A2B(b64))[0]
        except (ValueError, struct.error):
            continue
        if index < 0:  # 负通道号不会出现在结果里（对应原实现 range(width) 只覆盖 >=0）
            continue
        if index >= len(values):
            values.extend([_NAN] * (index + 1 - len(values)))
        values[index] = value
    return tuple(values)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    # 不用 Path.resolve(): 映射盘会被展开成 \\host\share UNC，sqlite 会把 host 当 URI authority。
    path = Path(db_path)
    if not path.is_absolute():
        path = path.absolute()
    posix = path.as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix  # windows 盘符 -> file:///Z:/...
    return sqlite3.connect("file://" + quote(posix, safe="/:") + "?mode=ro", uri=True)


def _config_json(con: sqlite3.Connection, name: str) -> dict:
    row = con.execute(
        "SELECT JsonText FROM Config WHERE PropertyName = ? ORDER BY Id DESC LIMIT 1", (name,)
    ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return {}


def _load_markers(con: sqlite3.Connection, rec: Recording, names: list) -> None:
    """标记表：新版 Marker(Name, CameraTime)，旧版 markers(marker_type, camera_clock)。

    旧版 markers 没有名字列，marker_type 是 iom_settings 里那一行标记通道的 id
    （实测 marker_type=11 -> iom_settings.id=11 -> "Manual_3"），据此还原名字。
    """
    try:
        if "Marker" in names:
            rows = con.execute("SELECT Name, CameraTime FROM Marker ORDER BY Id")
            rec.markers = [(str(n), int(t)) for n, t in rows if t]
        elif "markers" in names:
            label_by_id = {}
            try:
                for row_id, iom_type in con.execute("SELECT id, iom_type FROM iom_settings"):
                    if iom_type:
                        label_by_id[int(row_id)] = str(iom_type)
            except sqlite3.DatabaseError:
                pass
            rows = con.execute("SELECT sn, marker_type, camera_clock FROM markers ORDER BY sn")
            for sn, mt, t in rows:
                if not t:
                    continue
                name = label_by_id.get(int(mt)) if mt is not None else None
                rec.markers.append((name or f"Marker{int(sn)}", int(t)))
    except sqlite3.DatabaseError:
        pass


# --------------------------------------------------------------------------- #
# 新版 schema：AllChannelRecord<n> + Value(b64) + Config(JsonText)
# --------------------------------------------------------------------------- #

def _load_current(con: sqlite3.Connection, rec: Recording, tables: list) -> None:
    frames = rec.frames
    append = frames.append
    wl_by_type = WAVELENGTH_BY_TYPE  # 局部绑定：主循环里每帧都要查
    decode = decode_values
    frame_cls = Frame
    for table in tables:
        cur = con.execute(f'SELECT Type, CameraTime, Value FROM "{table}" ORDER BY Id')
        for type_val, camera_time, value in cur:
            wavelength = wl_by_type.get(type_val)
            if wavelength is None:  # -1 Input 等，跳过
                continue
            vals = decode(value)
            if not vals:
                continue
            append(frame_cls(int(camera_time or 0), wavelength, vals))

    settings = _config_json(con, "CameraSignalSettings")
    rec.channels = settings.get("CameraChannels") or []
    rec.light_mode = settings.get("LightMode") or []
    rec.record_mode = settings.get("RecordMode") or {}
    rec.sampling = float(settings.get("Sampling") or 10.0)

    events = _config_json(con, "EventSettings")
    rec.event_names = [str(ch.get("Name")) for ch in (events.get("Channels") or [])
                       if ch.get("Name")]


# --------------------------------------------------------------------------- #
# 旧版 schema：sig-<波长>nm 分表 + experiment_settings 键值对
# --------------------------------------------------------------------------- #

_LEGACY_SIG_RE = re.compile(r"^sig-(\d+)nm$", re.IGNORECASE)


def _legacy_settings(con: sqlite3.Connection) -> dict:
    """experiment_settings 是 section/content 两列的键值表，键统一转小写。"""
    try:
        rows = con.execute("SELECT section, content FROM experiment_settings").fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {str(k).strip().lower(): v for k, v in rows if k}


def _legacy_channel_count(con: sqlite3.Connection, tables: list, settings: dict) -> int:
    """优先用 channel_count；缺失时退化为扫描实际有值的最高 channel_<n>。"""
    try:
        n = int(float(settings.get("channel_count") or 0))
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n
    n = 0
    for table in tables:
        for col in (r[1] for r in con.execute(f'PRAGMA table_info("{table}")')):
            if not col.startswith("channel_"):
                continue
            try:
                idx = int(col.split("_", 1)[1])
            except ValueError:
                continue
            if idx <= n:
                continue
            if con.execute(f'SELECT 1 FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT 1').fetchone():
                n = idx
    return n or 1


def _load_legacy(con: sqlite3.Connection, rec: Recording, tables: list) -> None:
    settings = _legacy_settings(con)
    n_ch = _legacy_channel_count(con, tables, settings)
    rec.channels = []  # 旧版 db 不存 ROI 框，meta 里的 Channels 只能填 0

    frames = rec.frames
    append = frames.append
    frame_cls = Frame
    col_sql = ", ".join(f'"channel_{i}"' for i in range(1, n_ch + 1))
    for table in tables:  # 波长直接写在表名里
        wavelength = int(_LEGACY_SIG_RE.match(table).group(1))
        for row in con.execute(f'SELECT camera_clock, {col_sql} FROM "{table}" ORDER BY sn'):
            vals = tuple(_NAN if v is None else float(v) for v in row[1:])
            append(frame_cls(int(row[0] or 0), wavelength, vals))

    # frame_rate 是相机总帧率（多光源交替），每光源采样率 = 总帧率 / 光源数
    n_light = len(tables) or 1
    try:
        total_fps = float(settings.get("frame_rate") or 0)
    except (TypeError, ValueError):
        total_fps = 0.0
    rec.sampling = total_fps / n_light if total_fps > 0 else 10.0

    # 拼成新版 LightMode 的结构，build_meta 无需分支
    light_mode = []
    for i in range(8):
        wave = settings.get(f"light{i}_wave_length")
        if not wave:
            break
        light_mode.append({
            "WaveType": wave,
            "LightPower": settings.get(f"light{i}_power", 0),
            "IsChecked": str(settings.get(f"light{i}_recorded", "False")).strip().lower() == "true",
        })
    rec.light_mode = light_mode
    rec.record_mode = {
        "IsContinuous": str(settings.get("mode", "")).strip().lower() != "discontinuous",
    }


def load_recording(db_path: str | Path) -> Recording:
    """读取一个 data.db，返回 Recording（帧按时间升序）。新旧 schema 自动判别。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"data.db not found: {db_path}")

    con = _connect_ro(db_path)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        tables = [n for n in names if n.startswith("AllChannelRecord")]
        legacy = sorted(n for n in names if _LEGACY_SIG_RE.match(n))
        if not tables and not legacy:
            raise ValueError(
                f"{db_path} looks like an Inper db but has neither AllChannelRecord "
                f"nor sig-<wavelength>nm table (tables: {', '.join(names) or 'none'})"
            )

        rec = Recording(db_path=db_path)
        if tables:
            _load_current(con, rec, tables)
        else:
            _load_legacy(con, rec, legacy)

        rec.frames.sort(key=itemgetter(0))  # NamedTuple 第 0 项即 t_ns
        if rec.frames:  # 已按时间排序，首尾即最值，不必再扫一遍
            rec._t0_ns = rec.frames[0].t_ns
            rec._t_end_ns = rec.frames[-1].t_ns

        _load_markers(con, rec, names)
        return rec
    finally:
        con.close()


if __name__ == "__main__":  # 冒烟自检：python inper_db.py <session_dir>
    import sys

    rec = load_recording(Path(sys.argv[1]) / "data.db")
    print(f"frames      : {len(rec.frames):,}")
    print(f"wavelengths : {rec.wavelengths}")
    print(f"channels    : {[c.get('Name') for c in rec.channels]}")
    print(f"sampling    : {rec.sampling} Hz / light  -> fps {rec.fps}")
    print(f"duration    : {rec.duration_s() / 3600:.2f} h")
    print(f"markers     : {rec.markers[:5]}{' ...' if len(rec.markers) > 5 else ''}")
    print(f"events      : {rec.event_names}")
