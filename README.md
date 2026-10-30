# RWD Fiber Pretreatment

A standalone, interactive pretreatment tool for RWD fiber photometry recordings. Open one
recording folder, fit and remove the bleaching decay of every available wavelength, combine the
analysis and reference channels the way the RWD software does, compute dF/F0 and Z-score traces,
run marker-aligned event analysis, and export figures, tables and AAPlot-compatible text files.
Input files are opened read-only and are never modified.

## Install and start

- A Python 3 installation, or Anaconda on Windows.
- `pip install -r requirements.txt` (`matplotlib`, `numpy`, `pandas`, `scipy`).
- Start with `run_RWD_fiber_pretreatment.bat`, or `python app.py` in a prepared environment.

On Windows the launcher self-checks the environment: it locates Anaconda, creates the `aaplot`
conda environment (Python 3.12) when missing, verifies every package in `requirements.txt`
through `env_check.py`, installs what is missing with `pip`, and starts `app.py` with that
environment's interpreter. `--no-install` only reports the check result, `--recreate` rebuilds
the environment, and `AAPLOT_NO_PAUSE=1` skips the final pause for automation.

## Input folder

`Fluorescence.csv` is required. Its first line is free-form metadata and is skipped; the table
starts on the second line.

- `TimeStamp` (milliseconds) is required, plus at least one column named `CH<n>-410`,
  `CH<n>-470` or `CH<n>-560`, where `<n>` is the acquisition channel. All three wavelengths are
  first-class: 410, 470 and 560 each get their own offset, bleaching fit, correction, dF/F0,
  Z-score, plot and export. Any subset per channel is valid, and missing controls are simply
  hidden.
- `Events.csv` is optional. Rows with `State` equal to `1` become markers; if `State` is missing
  or selects nothing, every row is used. The marker name comes from the `Name` column.
- Rows without a valid `TimeStamp` are dropped and the table is sorted by time. At least 20
  valid samples are required.
- A short run of empty or non-numeric cells in a fluorescence column — up to 3 samples, about
  0.1 s at 30 Hz — is **bridged on load** by linear interpolation between its neighbours, so the
  sample count, the timestamps and every derived trace stay unchanged and the fit never sees a
  hole. Longer runs are **not** bridged: they stay NaN, are reported as a gap, and are left out
  of the fit. Both cases are stated in the status line and recorded under
  `fit_details.gap_fills` in the saved JSON.

Markers can be added, deleted or restored in **Markers (Working Copy)** on the **Data & Fitting**
tab. Edits are kept in memory and written to `markers_working_copy.csv` on export.

## Workflow

1. **Data & Fitting** — click **Select Recording Folder**, choose the **Recording Channel**, set
   the **Valid Data Range**, then enter each visible **Offset** and **Fitting Baseline**. The
   baseline is the **lowest point of the trace after bleaching** — the plateau the signal
   decays to — read off the **raw** plot, so it still contains the offset: the program subtracts
   the offset from it and puts the corrected trace on that level, so leave **Keep these windows
   out of the bleaching fit** ticked and the marker windows cannot pull the baseline towards the
   responses. dF/F0 is then normalised by the
   mean of the corrected trace inside the baseline window, so the two fields act through the
   level they set. A baseline that is not larger than the offset is rejected with an error naming
   the channel, because nothing would be left of it once the offset is removed.
2. Open **Configure <wavelength> Fit...** for every wavelength, select one or more regions and a
   model, preview, and confirm. The window suggests a model under the model list — it reads how
   much the decay fades, how straight it is in log space and how many time constants fit in the
   window — with a **Use Suggested Model** button next to the advice.
3. Pick the **Bleaching Model**. `additive` (default) subtracts the fitted decay and keeps a
   response the same size however far the trace has faded; `multiplicative` divides the decay out,
   which is what the probe's own photobleaching does and keeps a response the same *relative* size
   at 1 min and at 20 min. See [docs/fitting.md](docs/fitting.md) for the two formulas and a worked
   example.
4. Choose the **Analysis Wavelength** and, optionally, a **Reference Wavelength** with a
   **Combine Channels** mode. With a reference the default `rwd` mode fits and subtracts the
   motion contribution; with `None` the independently corrected analysis channel is used.
5. Click **Apply Correction**. Every available wavelength is corrected independently.

Then use **Full trace analysis** for dF/F0 and Z-score, **Event Analysis** for marker-aligned
trials, and **Export Results** to write the folder.

## Defaults at a glance

| Setting | Default | Where |
| --- | --- | --- |
| Channel combination | `rwd` (RWD motion fit) | Data & Fitting |
| F0 for dF/F0 | mean of the corrected trace in the baseline window | Full trace analysis |
| Fit model | double exponential | Configure Fit |
| PLS `beta` | 8, allowed 5–12 | Configure Fit |
| Smoothing / downsampling | off | Full trace analysis |
| Event window | 5 s before, 30 s after, 5 s baseline | Event Analysis |
| Event Z-score SD | one per trial, from that trial's own pre-marker baseline (pooled optional); the trial average also gets `zscore_of_average`, which is about √n times larger | Event Analysis |
| Estimate exclusion window | 5 s before, 60 s after each marker | Data & Fitting |
| Time units | per field (`s` for windows, `min` for ranges), switchable | every time field |

## Documentation

| Page | Contents |
| --- | --- |
| [docs/fitting.md](docs/fitting.md) | Bleaching correction formula, fitting models, PLS fit, fit-window behaviour |
| [docs/channels.md](docs/channels.md) | The three channel combinations, the RWD motion fit, choosing and estimating the reference weight |
| [docs/analysis.md](docs/analysis.md) | Display processing, dF/F0/Z-score baselines, event analysis, exported files |
| [docs/restore.md](docs/restore.md) | Restoring a previous analysis and what gets refitted |
| [docs/updates.md](docs/updates.md) | Changelog, newest first |

## Output

Each export creates a new folder named by **Output Folder Name** (an existing name gets `_2`,
`_3`, ...). Depending on the selected boxes it contains corrected, dF/F0 and Z-score traces as
CSV/PNG/SVG, per-marker event trial and average tables, event overview figures and heatmaps,
`markers_working_copy.csv`, `parameters_and_marker_edits.json` (full settings log), and
AAPlot-compatible `Time,Signal,Marker` text files. See [docs/analysis.md](docs/analysis.md) for
the exact file list.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| A dialog appears during apply or plotting | The full traceback is also written to `aaplot_errors.log` beside `app.py`, appended, never overwritten — send that file. |
| `Series of different length were trimmed ...` in the status line | Two series reached a plot with different lengths; they are trimmed and reported instead of aborting the redraw. Usually an empty cell in the recording. |
| `6 cells in CH1-410 left as a gap` | More than 3 consecutive samples are missing. The trace keeps a gap there; fill or remove those rows in `Fluorescence.csv` if they are not a real dropout. |
| Apply does nothing / asks to confirm regions | A data-range, model or baseline-mode change invalidates that channel's confirmed fit. Offset and baseline edits do not. |
| Event results vanish after changing the combination | Trials are computed from the stored combination trace; they are recomputed on the next apply, and cleared if the combination itself changed. |
| A single-wavelength recording | Set **Reference Wavelength** to `None`; the combination is skipped and the corrected channel is analysed on its own. |
| 470 and 410 move in opposite directions during events, and no weight gives a bigger response | Click **Check Reference Polarity**. If 410 carries its own event-locked artifact, or mirrors the signal, no linear combination can recover more: report the analysis channel alone (the window offers that as one button) — see `docs/channels.md`. |
| `The fitting baseline for 470 (...) must be larger than the offset` | The baseline is the **raw** level, offset included; F0 = baseline − offset has to stay positive. Enter the level you read off the raw trace, or correct the offset. |
| dF/F0 comes out about twice too large or too small | Check that the **Fitting Baseline** is the raw level of the trace minimum and not an already-subtracted one: F0 is the denominator, so a wrong baseline scales every dF/F0 by the same factor. |

## Author and development

Developed by **Ruyi Cai**, **Sophielothia** and **nexsrust** at **Peking University**, with
development assistance from **OpenAI Codex** and **DeepSeek Harness**.

---

# RWD 光纤记录预处理（中文版）

> 本节是上面英文说明的中文翻译，内容一一对应。界面上的按钮与字段名是英文的，这里保留原文并加粗，方便对着界面找。

面向 RWD 光纤记录（fiber photometry）的独立、交互式预处理工具。打开一个记录文件夹，对每个可用波长分别拟合并去除漂白衰减，按 RWD 软件的方式合并分析通道与参考通道，计算 dF/F0 与 Z-score 曲线，做 marker 对齐的事件分析，并导出图片、表格和 AAPlot 兼容的文本文件。**输入文件以只读方式打开，从不修改。**

## 安装与启动

- 已安装 Python 3，或 Windows 上的 Anaconda。
- `pip install -r requirements.txt`（`matplotlib`、`numpy`、`pandas`、`scipy`）。
- 用 `run_RWD_fiber_pretreatment.bat` 启动，或在准备好的环境里执行 `python app.py`。

在 Windows 上，启动脚本会自检环境：定位 Anaconda，缺少 `aaplot` conda 环境（Python 3.12）时创建它，通过 `env_check.py` 逐项核对 `requirements.txt` 里的包，用 `pip` 装上缺的，然后用该环境的解释器启动 `app.py`。`--no-install` 只报告检查结果，`--recreate` 重建环境，`AAPLOT_NO_PAUSE=1` 跳过最后的暂停（用于自动化）。

## 输入文件夹

`Fluorescence.csv` 是必需文件。第一行是自由格式的元数据，会被跳过；数据表从第二行开始。

- 必需 `TimeStamp`（毫秒），以及至少一个名为 `CH<n>-410`、`CH<n>-470` 或 `CH<n>-560` 的列，`<n>` 是采集通道号。三个波长都是一等公民：410、470、560 各自拥有独立的 offset、漂白拟合、校正、dF/F0、Z-score、绘图和导出。每个通道可以只启用其中任意几个波长，缺的对照通道直接隐藏。
- `Events.csv` 可选。`State` 等于 `1` 的行会成为 marker；若没有 `State` 列或筛选后为空，则使用全部行。marker 名称取自 `Name` 列。
- `TimeStamp` 无效的行会被丢弃，表按时间排序。至少需要 20 个有效采样点。
- 荧光列中一小段空白或非数字单元格——最多 3 个采样点，30 Hz 下约 0.1 s——会在**载入时**用相邻值线性插值补齐，因此采样点数、时间戳和所有派生曲线都保持不变，拟合也看不到空洞。更长的一段**不会**补齐：保持 NaN，作为缺口上报，并排除在拟合之外。两种情况都会写在状态栏，并记录到保存 JSON 的 `fit_details.gap_fills` 下。

marker 可在 **Data & Fitting** 标签页的 **Markers (Working Copy)** 里增加、删除或恢复。编辑只存在内存中，导出时写入 `markers_working_copy.csv`。

## 工作流程

1. **Data & Fitting** —— 点 **Select Recording Folder** 选择记录文件夹，选 **Recording Channel**，设置 **Valid Data Range**，然后为每个可见通道填 **Offset** 和 **Fitting Baseline**。这里的 baseline 是**漂白后曲线的最低点**——信号最终衰减到的平台值——从**原始**曲线上读出，因此它仍包含 offset：程序会从中减掉 offset，并把校正后的曲线放在这个水平上，所以请保持 **Keep these windows out of the bleaching fit** 勾选，marker 窗口就不会把基线往响应方向拉。dF/F0 随后用基线窗内校正曲线的均值归一化，也就是说这两个字段是通过它们设定的水平起作用的。baseline 不大于 offset 会被拒绝，并报出具体通道名，因为减掉 offset 之后什么都不剩了。
2. 为每个波长打开 **Configure <wavelength> Fit...**，选一个或多个区间和模型，预览，然后确认。窗口会在模型列表下方给出模型建议——它读取衰减幅度、对数空间里的直线程度，以及窗口内能容纳几个时间常数——旁边有 **Use Suggested Model** 按钮可直接采用。
3. 选择 **Bleaching Model**。`additive`（默认）减去拟合出的衰减，响应大小不随漂白程度变化；`multiplicative` 把衰减除掉，这正是探针自身光漂白的情形，能让 1 min 和 20 min 处**相对幅度相同**的响应输出一致。两种公式和一个算例见 [docs/fitting.md](docs/fitting.md)。
4. 选择 **Analysis Wavelength**，可选地设置 **Reference Wavelength** 和 **Combine Channels** 模式。有参考通道时默认的 `rwd` 模式会拟合并减去运动成分；设为 `None` 则只使用独立校正后的分析通道。
5. 点 **Apply Correction**。每个可用波长分别独立校正。

之后用 **Full trace analysis** 算 dF/F0 与 Z-score，用 **Event Analysis** 做 marker 对齐的 trial，用 **Export Results** 写出整个文件夹。

## 默认值一览

| 设置 | 默认 | 位置 |
| --- | --- | --- |
| 通道合并方式 | `rwd`（RWD 运动拟合） | Data & Fitting |
| dF/F0 的 F0 | 基线窗内校正曲线的均值 | Full trace analysis |
| 拟合模型 | 双指数 | Configure Fit |
| PLS `beta` | 8，允许 5–12 | Configure Fit |
| 平滑 / 降采样 | 关闭 | Full trace analysis |
| 事件窗口 | marker 前 5 s、后 30 s、基线 5 s | Event Analysis |
| 事件 Z-score 的 SD | 每个 trial 一个，取自该 trial 自己的 marker 前基线（可选跨 trial 合并）；trial 平均另给 `zscore_of_average`，约为 √n 倍 | Event Analysis |
| 估算排除窗 | 每个 marker 前 5 s、后 60 s | Data & Fitting |
| 时间单位 | 按字段各自设定（窗口用 `s`，范围用 `min`），可切换 | 每个时间字段 |

## 文档

| 页面 | 内容 |
| --- | --- |
| [docs/fitting.md](docs/fitting.md) | 漂白校正公式、拟合模型、PLS 拟合、拟合窗口行为 |
| [docs/channels.md](docs/channels.md) | 三种通道合并方式、RWD 运动拟合、参考权重的选择与估算 |
| [docs/analysis.md](docs/analysis.md) | 显示处理、dF/F0 与 Z-score 基线、事件分析、导出文件 |
| [docs/restore.md](docs/restore.md) | 恢复以往的分析，以及哪些通道会被重新拟合 |
| [docs/updates.md](docs/updates.md) | 更新日志，最新的在前 |

## 输出

每次导出都会新建一个以 **Output Folder Name** 命名的文件夹（重名会加 `_2`、`_3`……）。按所勾选的项，其中包含校正曲线、dF/F0 和 Z-score 曲线的 CSV/PNG/SVG，按 marker 分的事件 trial 表与平均表，事件总览图与热图，`markers_working_copy.csv`，`parameters_and_marker_edits.json`（完整参数日志），以及 AAPlot 兼容的 `Time,Signal,Marker` 文本文件。完整文件清单见 [docs/analysis.md](docs/analysis.md)。

## 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 应用或绘图时弹窗报错 | 完整 traceback 同时追加写入 `app.py` 同目录下的 `aaplot_errors.log`（只追加，不覆盖），把这个文件发过来即可。 |
| 状态栏出现 `Series of different length were trimmed ...` | 有两条曲线以不同长度进入绘图；程序会截齐并上报，而不是中断重绘。通常是记录中有空单元格。 |
| `6 cells in CH1-410 left as a gap` | 连续缺失超过 3 个采样点。曲线在那里保留缺口；若并非真实断信号，请在 `Fluorescence.csv` 里补齐或删除这些行。 |
| 点 Apply 没反应 / 要求重新确认区间 | 数据范围、模型或基线模式的改动会令该通道已确认的拟合失效。只改 offset 和 baseline 不会。 |
| 改了合并方式后事件结果消失 | trial 是从存下来的合并曲线算的；下一次 apply 会重算，若合并方式本身变了则清空。 |
| 只有单波长的记录 | 把 **Reference Wavelength** 设为 `None`；合并被跳过，直接分析校正后的通道。 |
| 事件期间 470 与 410 反向移动，任何权重都得不到更大的响应 | 点 **Check Reference Polarity**。若 410 携带自己的事件锁定伪影，或只是镜像了信号，任何线性组合都无法还原更多——单独报告分析通道即可（窗口里有一个按钮直接这么做），详见 `docs/channels.md`。 |
| `The fitting baseline for 470 (...) must be larger than the offset` | baseline 是**原始**水平、含 offset；F0 = baseline − offset 必须保持为正。请填从原始曲线上读到的值，或修正 offset。 |
| dF/F0 大约偏大或偏小一倍 | 检查 **Fitting Baseline** 是否是原始曲线最低点、而不是已经减过 offset 的值：F0 是分母，基线错了会让整条 dF/F0 按同一倍数缩放。 |

## 作者与开发

由**北京大学**的 **Ruyi Cai**、**Sophielothia** 和 **nexrsrust** 开发，开发过程中使用了 **OpenAI Codex** 与 **DeepSeek Harness** 的协助。
