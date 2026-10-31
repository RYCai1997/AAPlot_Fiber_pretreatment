# InjectionFinder — 光纤记录注射时间自动标注

从光纤记录（Fluorescence.csv）里找出**注射时刻**，并在源目录下生成一套带 marker 的副本 + 一张复核图。

原理：注射/保定/抓取这类操作会在光纤上产生**四个通道同时、等幅、高频**的运动伪影。脚本按「去漂移 → 高频能量 → 四通道归一化平均」算出伪影指数，
在预设的注射时间窗（默认记录开始后 30–90 min）里找最强的事件簇，把它标成注射窗。

## 依赖

Python 3.8+，`numpy`、`pandas`、`Pillow`；GUI 需要标准库 `tkinter`（Windows 官方安装包自带）。
不需要 matplotlib，图由 Pillow 手绘。

## 启动

| 平台 | 双击这个文件 | 说明 |
| --- | --- | --- |
| **Windows** | `run_InjectionFinder.bat` | 自动挑选装了 numpy / pandas / Pillow / tkinter 的 Python，缺依赖先装，然后打开界面 |
| **macOS** | `run_InjectionFinder.command` | 复用主程序那份虚拟环境，仅在导入失败时安装依赖，然后打开界面 |

两个启动器做的事相同，细节分别见下。

### Windows

- 双击 **`run_InjectionFinder.bat`** 启动界面：自动挑选装了 numpy / pandas / Pillow / tkinter 的 Python；
  缺依赖会先自动 `pip install`（默认源失败自动换清华镜像）。
- 只检测不启动：双击 **`setup_env.bat`**，列出本机每个 Python 的依赖状态，缺什么装什么。
- 不建议直接双击 `.py`：`.py` 交给系统 `py.exe`，它未必选中装了依赖的解释器
  （本机 `py` 默认是 Python 3.9，没有 numpy，双击会一闪就退）。

### macOS

- 双击 **`run_InjectionFinder.command`**：找一个确实带 `tkinter` 的 Python 3.9+，复用主程序那份虚拟环境
  （`~/Library/Application Support/AAPlotFiberPretreatment/venv`，没有就建），只在导入失败时安装 `requirements.txt`。
  首次下载的副本若无执行权限，先 `chmod +x run_InjectionFinder.command`；被「门禁」拦住时右键 →「打开」一次。
  也可在终端执行 `./run_InjectionFinder.command`。
- 命令行模式照样走启动器，参数原样转发：
  `./run_InjectionFinder.command --nogui --fluo Fluorescence.csv --events Events.csv`
- Mac 上没有 `run_InjectionFinder.bat` / `setup_env.bat`；不建环境、不装依赖用 `--no-install`（依赖齐全时照常打开界面），`--recreate` 重建虚拟环境，
  `AAPLOT_VENV_DIR` 可改位置，`AAPLOT_NO_PAUSE=1` 跳过结尾暂停。
- 出图的中文字体在 macOS 上用冬青黑体（Hiragino Sans GB）/ Arial Unicode / 宋体，界面字体用苹方 + Menlo。
  `PingFang.ttc` 是字体集合，Pillow 打不开，所以没有列进候选。

> `.py` 已加保护：缺依赖会弹中文提示并给出 pip 命令；运行期未捕获异常写入同目录 `injection_finder_crash.log`。

## 用法

### 图形界面

```
python injection_marker_gui.py
```

选 `Fluorescence.csv`（必填）、`Events.csv`（可空）、行为数据 `FreezingOutput.csv`（可选，给了才画 C 面板）
→ 设搜索窗口 / 阈值 → 「开始分析并写出」。输出目录留空时写到 **源文件所在目录下的 `injection_marker\`**。

### 命令行 / 批处理

```
python injection_marker_gui.py --nogui --fluo <Fluorescence.csv> --events <Events.csv> [--motion <FreezingOutput.csv>]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--fluo` | — | Fluorescence.csv（必填） |
| `--events` | 空 | 原生 Events.csv；不给则新建一个只含 marker 的文件 |
| `--motion` | 空 | Any-maze FreezingOutput.csv，给了才画 C 面板 |
| `--outdir` | 源目录下 `injection_marker\` | 输出目录；与源文件重合时**直接拒绝执行** |
| `--start` / `--end` | 30 / 90 | 搜索窗口（min） |
| `--thr` | 3.0 | 伪影指数阈值（× 各通道基线中位数） |
| `--dur` | 1.0 | 事件最短持续（s） |
| `--name` | Injection | marker 名前缀 |
| `--mode` | start_end | `start_end` 写 Start/End 两个 marker；`single` 只在窗口中心写一个 |
| `--time-unit` | auto | 时间戳单位 `auto` / `ms` / `s` |

给了 `--nogui` 或 `--fluo` 就是批处理模式：参数/路径/窗口问题只在 stderr 打一行 `错误：…` 并以退出码 1
结束，**不弹窗**（弹窗是模态的，会把调用它的脚本卡在那儿等人点 OK）；只有真正没预料到的异常才写
`injection_finder_crash.log`。正常结束时退出码 0。

## 输出（三个文件）

- `Fluorescence.csv` — 逐行照抄源文件（二进制级：编码、BOM、换行方式全保留），只把 marker 时刻所在数据行的 `Events` 字段填入 marker 名
- `Events.csv` — 原内容保留 + 追加 marker 行（ms 时间戳，与上面 CSV 命中行完全一致）
- `injection_window.png` — A 全程伪影指数（30–90 min 预设窗底色）/ B 候选窗四通道放大 / C 行为视频运动量（有行为数据时）

## 数据安全

- 源文件只以只读方式打开，任何情况下都不会被写入
- 输出路径与源文件重合（例如 `--outdir` 指向源目录）时抛 `RuntimeError` 拒绝执行
- 覆盖已有结果文件前，GUI 会弹确认框

## 算法

1. 原始四通道插值到 10 Hz 均匀网格
2. 每通道减去 5-min 滑动中位数（去漂移/ bleaching）
3. 取 2-s 滑动标准差作为高频能量
4. 各通道除自身全局中位数后四通道平均 = 伪影指数
5. 阈值穿越成段（间隔 < 0.5 s 合并），丢弃短于 `--dur` 的段
6. 搜索窗口内按 60 s 间隙聚成簇，取峰值最大的簇，起止裁剪到窗口内

四通道（CH1/CH2 × 410/470）同时等幅抖动是关键判据：真实钙信号不会让 410 与 470 同幅同相跳变。

## 实测结果（C19_10mpk_THC，14.46 h，THC 10 mpk i.p.）

- 候选窗 **79.12–79.99 min**（52.1 s），脉冲峰值 **42.0× 基线** @ 79.44 min，9 个事件段
- `InjectionStart` @ 4 747 076.639 ms，`InjectionEnd` @ 4 799 177.479 ms
- 输出 CSV 与源文件逐行比对**仅这 2 行不同**
- 行为视频交叉验证：全记录最大运动尖峰出现在 79.75–79.95 min，与候选窗后半段重合

例图：`examples/C19_10mpk_THC/injection_window.png`，对应 Events：`examples/C19_10mpk_THC/Events.csv`。

## 测试

```
python tests/run_checks.py        # 边界：LF/CRLF/GBK 源、坏行、空 Events、无候选、反向区间、输出=源目录
python tests/run_gui_checks.py    # GUI：正常路径 / 异常路径（按钮状态、日志、弹窗）
```

两个脚本都会自己合成小数据，不需要真实记录；产物写在 `tests/_tmp/`。

## 已知限制

- 注射动作本身无法从光学信号定位到秒级；输出的是「操作窗口」（通常几十秒），不是针尖时刻
- 伪影指数依赖 4 个通道都存在；缺失通道的记录需要先改 `compute_artifact_index`
- 记录末尾取下光纤也会产生巨大伪影，但它落在搜索窗口之外，不影响结果
