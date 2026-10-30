# InjectionFinder — 光纤记录注射时间自动标注

从光纤记录（Fluorescence.csv）里找出**注射时刻**，并在源目录下生成一套带 marker 的副本 + 一张复核图。

原理：注射/保定/抓取这类操作会在光纤上产生**四个通道同时、等幅、高频**的运动伪影。脚本按「去漂移 → 高频能量 → 四通道归一化平均」算出伪影指数，
在预设的注射时间窗（默认记录开始后 30–90 min）里找最强的事件簇，把它标成注射窗。

## 依赖

Python 3.8+，`numpy`、`pandas`、`Pillow`；GUI 需要标准库 `tkinter`（Windows 官方安装包自带）。
不需要 matplotlib，图由 Pillow 手绘。

## Windows 双击启动

- 双击 **`run_InjectionFinder.bat`** 启动界面：自动挑选装了 numpy / pandas / Pillow / tkinter 的 Python；
  缺依赖会先自动 `pip install`（默认源失败自动换清华镜像）。
- 只检测不启动：双击 **`setup_env.bat`**，列出本机每个 Python 的依赖状态，缺什么装什么。
- 不建议直接双击 `.py`：`.py` 交给系统 `py.exe`，它未必选中装了依赖的解释器
  （本机 `py` 默认是 Python 3.9，没有 numpy，双击会一闪就退）。

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
