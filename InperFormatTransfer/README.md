# InperFormatTransfer

把 InperView（瑞沃德）记录仪的 `data.db` 转成 InperView 自己「导出荧光数据」产出的 4 个 CSV，
供 AAPlot_Fiber_pretreatment 及下游脚本直接读取，无需再手工点一次导出。

## 文件

| 文件 | 作用 |
| --- | --- |
| `inper_db.py` | data.db 解析层（SQLite 只读），`load_recording()` 返回帧/标记/配置 |
| `inper_transfer.py` | 命令行入口，产出 CSV |
| `inper_transfer_gui.py` | 图形界面（tkinter），选输入/输出/是否跳过 unaligned |
| `run_inper_transfer.bat` | 双击启动 GUI（Windows） |
| `run_inper_transfer.command` | 双击启动 GUI（macOS） |

无第三方依赖，Python 3.9+ 自带 `sqlite3` 即可（GUI 需自带 tkinter 的 Python）。

## GUI

双击 `run_inper_transfer.bat`（macOS 双击 `run_inper_transfer.command`），或 `python inper_transfer_gui.py`。

macOS 的启动器复用主程序那份虚拟环境（`~/Library/Application Support/AAPlotFiberPretreatment/venv`，没有就建），
因为本工具只用标准库，所以正常情况下不会联网装任何东西；`--no-install` 只做检查，`--recreate` 重建环境。
界面字体在 macOS 上用苹方（PingFang SC）+ Menlo，Windows 上仍是 Segoe UI + Consolas。

- **Session folder**：选含 `data.db` 的 session 文件夹；也可以点 `data.db…` 直接选那个文件（自动取父目录）
- **Output folder**：留空 = 把 CSV 写回 session 文件夹；也可另选目录
- **Skip Fluorescence-unaligned.csv**：勾选则不写 32 MB 的原始帧文件
- **Replace existing output files**：勾选直接覆盖；不勾选时若输出已存在会弹窗确认
- 转换在后台线程执行（界面不卡），Log 显示每 5 万行的进度和最后的校验摘要，完成后可点 `Open output folder`

## 命令行

```bash
# 单个 session：默认写回 session 目录；已存在同名输出则跳过（打印 [skip]）
python inper_transfer.py "Z:\...\C19_10mpk_THC"

# 指定输出目录 / 覆盖已有文件 / 省掉 unaligned（32 MB/session）
python inper_transfer.py "Z:\...\C19_10mpk_THC" -o D:\out --overwrite --skip-unaligned

# 批量：递归找 ROOT 下所有 data.db，逐个转换
python inper_transfer.py --batch "Z:\...\female DLS and NAcSh 2025"

# 网络盘默认先把 data.db 整块复制到本地临时目录再解析（逐页读网络盘慢 10 倍以上）；
# 加 --in-place 就地读取，不占临时空间但明显更慢
python inper_transfer.py "Z:\...\C19_10mpk_THC" --in-place
```

每个 session 完成后打印帧数、时长、对齐行数、marker 数，以及列名/时间戳/行数校验结果。

## 输出

| 文件 | 内容 |
| --- | --- |
| `Fluorescence.csv` | 第 1 行 meta（`;` 分隔的伪 JSON），第 2 行表头，之后 10 Hz 对齐数据：`TimeStamp,Events,CH1-410,CH1-470,CH2-410,CH2-470,` |
| `Fluorescence-unaligned.csv` | 20 fps 原始帧，每帧一行，`Lights` 列标 410/470 |
| `Events.csv` | `TimeStamp,Name,State`，每个标记一行 `State=1` |
| `Outputs.csv` | 仅表头（db 无数字输出通道） |

对齐规则：以出现次数最多的波长为主轴（通常 470 或 410 之一），每个其它波长的帧与最近的主轴帧
一对一配对，允许偏差为采样周期的一半（10 Hz → 50 ms），配不上的留空。

## meta 字段来源

| 字段 | 来源 |
| --- | --- |
| `Led410/470/560Enable` | `Config.LightMode[].IsChecked` |
| `Led*Value` | `Config.LightMode[].LightPower` |
| `Excitation` | 固定 `mode=1, interval_time=20, continuous_time=20`（连续记录） |
| `Channels[].Roi1` | `CameraSetting.CameraChanel[].Left/Top/ROI` → `left;top;roi;roi` |
| `Channels[].Roi2` | 无来源，填与 Roi1 相同（见下） |
| `AllMarking` type=1 | 该 session 的 marker 名，按调色板依次取色 |
| `AllMarking` type=2 | `Input1..4` 固定名 + 固定色 |
| `Fps` | `2 × Config.Sampling.Rate` |

## 与 InperView 真实导出的差异（已知，不影响下游）

1. **Events.csv 行数**：InperView 每个标记写两行（`State=0` 按下 / `State=1` 松开），db 只存一个时刻，
   所以本脚本每个标记写一行 `State=1`。
2. **Roi2**：db 只存一组 ROI 框，真实导出的第二组（背景 ROI）无数据来源，本脚本填与 Roi1 相同。
3. **首帧波长**：`Fluorescence-unaligned.csv` 按原始帧时间排序，410/470 谁在前取决于该 session 实际帧序。

## 已做的校验

- C19_10mpk_THC（1,041,251 帧 / 14.46 h / 2 ROI，Z: 网络盘）：aligned 520,626 行、unaligned 1,041,251 行；
  时间戳、波长、通道值（4 位小数）、事件时刻逐项断言与 db 原始帧一致——全部通过。
- meta 行与 `C93_0.1mpk`（InperView 真实导出）逐键比对：键名、键顺序、输入通道颜色、`;` 分隔符、首尾花括号一致。
- 三种运行模式均已实测：默认写回、已存在时跳过、`--skip-unaligned --overwrite`。
- 优化前后输出逐字节一致：C16D1 的 4 个 CSV 用 MD5 比对全部相同；解码器另有 4008 组构造用例
  （2/3 通道、乱序 key、坏 b64、负 key、空串）新旧实现对拍，无差异。
- GUI 端到端实测（本地 77 MB 的 data.db 副本，勾选跳过 unaligned）：10.1 s 完成，Log 输出进度与校验摘要，
  状态行显示 `Done. Wrote <output folder>`。

## 性能

本地 88.8 MB / 124.7 万帧 / 2 通道 session 实测：

| 阶段 | 优化前 | 优化后 |
| --- | --- | --- |
| 解析 data.db（`load_recording`） | 9.4 s | 4.0 ~ 4.9 s |
| `Fluorescence.csv`（62.3 万行） | 3.8 s | 2.3 ~ 2.9 s |
| `Fluorescence-unaligned.csv`（124.7 万行） | 2.9 s | 2.1 ~ 2.7 s |
| 端到端合计 | 15.3 s | 9.0 ~ 11 s |

同一时段与旧版并排对比为 18.5 s → 11.4 s（约 1.6 倍）；绝对值随磁盘缓存和系统负载波动 ±20%。
主要改动：`Frame` 换成 `NamedTuple`、`Value` 解码走「定长 + `binascii.a2b_base64`」快路径、
`n_channels/wavelengths/t0_ns` 改为解析时缓存、对齐改为一次遍历分组、CSV 按 8192 行批量落盘。

## 支持的 data.db schema

`inper_db.load_recording()` 自动判别两种 schema，`inper_transfer.py` 无需任何开关：

| | 新版（2024-08 起） | 旧版（≈2023 及更早） |
| --- | --- | --- |
| 数据表 | `AllChannelRecord<n>`，`Type` 列标波长 | `sig-410nm` / `sig-470nm` 按波长分表 |
| 像素值 | `Value` 字段 base64 编码的 float64 | `channel_1..channel_9` 明文 REAL 列 |
| 时间列 | `CameraTime`（ns） | `camera_clock`（ns，两光源共用绝对时钟） |
| 配置 | `Config` 表（PropertyName / JsonText） | `experiment_settings` 表（section / content 键值对） |
| 通道数 | `CameraSignalSettings.CameraChannels` | `channel_count` |
| 采样率 | `CameraSignalSettings.Sampling`（每光源） | `frame_rate ÷ 光源数`（frame_rate 是相机总帧率） |
| 标记 | `Marker(Name, CameraTime)` | `markers(sn, camera_clock)`，无名字，导出为 `Marker<sn>` |

旧版只读 `sig-<波长>nm` 原始信号；`BLCPolynomialFittedsig-*`（软件自算漂白拟合）和 `MCorrected`
（运动校正）两张表不读取。旧版 db 不存 ROI 框，`Fluorescence.csv` 首行 meta 的
`Channels.Roi1/Roi2` 会填 `0.0;0.0;0.0;0.0`，不影响下游计算。

### 时间零点与 Fps

- **对齐表时间零点恒为 0.000**：`anchor_to_primary()` 把 `Fluorescence.csv` / `Events.csv` 的
  零点设为「主轴光源（驱动输出行的那束光，通常是 410）的第一帧」，与 InperView 自己的导出一致。
  旧版 db 的 `camera_clock` 是两光源共用的绝对时钟，若另一光源有一帧更早，直接取全体首帧会让
  首行变成 47.620 之类的非零值。不删帧，那条早到的帧仍进 unaligned 并参与配对。
- **`Fluorescence-unaligned.csv` 的零点仍是全体首帧**，因此当另一光源先开时，它与对齐表相差半个
  采样周期（实测 47.62 ms）。
- **meta 的 `Fps` 用实测帧率**：标称 `frame_rate` 可能偏离实际（2023-12-17 这批实测每光源
  95.240 ms ≈ 10.5 Hz、相机 21.0 fps，而 `experiment_settings.frame_rate` 写的是 20）。
  实测与标称差 ≤2% 时仍用标称值。
- 旧版 `markers.marker_type` 是 `iom_settings.id`，据此还原标记名（`marker_type=11 → Manual_3`）。

## 注意

- `data.db` 全程以 `?mode=ro` 只读打开，不修改任何原始文件。
- 单 session 输出合计约 55 MB（unaligned 占 32 MB）；写网络盘目标目录的耗时取决于网络带宽。
- 源文件在网络盘上时（盘符类型为 4 的映射盘或 UNC 路径），默认先整块复制到 `%TEMP%\inper_stage_*`
  再解析，结束后删除；`--in-place` 关闭该行为。临时目录需能放下一个 data.db（约 60 ~ 100 MB）。
- `--skip-unaligned` 只跳过写出，不会删除已存在的旧 `Fluorescence-unaligned.csv`。
