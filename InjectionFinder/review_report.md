# 审核报告：`injection_marker_gui.py`

审核范围：`D:\DeepseekHarness_WorkSpace\eCB_selective_sensor_paper\injection_marker_gui.py`（691 行，只读审核，未修改源文件）。
测试数据：`_probe\C19_10mpk_THC\`（Fluorescence.csv 520628 行 / Events.csv / 行为数据），另用合成小数据做边界测试；所有输出写 `_probe\review_out\`。

## 结论

**有条件可交付。** 核心算法与 CLI 单次转换在"输出目录安全"时与已验证期望逐字节一致（见下），可以交付；但必须先修 3 个严重问题：`--outdir` 可把源文件写成 0 字节（S1）、GUI 异常路径完全失效导致窗口"卡死"（S2）、GUI 日志跨线程操作 Tk（S3）。其余为一般/建议项。

与期望对照（run1，`--nogui` 复跑）：

| 期望 | 实测 | 结论 |
|---|---|---|
| 候选窗 79.12–79.99 min | 79.12–79.99 min | ✓ |
| 峰值约 42×基线 | 42.0× @ 79.44 min | ✓ |
| 只 2 行不同 | 仅 line 47472 / 47993 不同 | ✓ |
| 行数 520628 一致 | 两侧 520629（按 `\n` 切分，含尾空串） | ✓ |
| 元数据+表头逐字节相同 | True | ✓ |
| Events.csv 追加 2 行 ms | `4747100.000,InjectionStart,1` / `4799200.000,InjectionEnd,1` | ✓ |
| 与既有 `_probe\test_out\` 一致 | 两文件 md5 完全相同 | ✓ |

`--motion` 三面板图另跑一次（`_probe\review_out\run2_motion\`），第三面板运动峰与 B 面板候选窗时间高度重合，行为数据对齐正确；中文渲染正常。

## 问题清单

### 严重（bug）

**S1 `--outdir` 指向源目录时把源文件覆盖为 0 字节**（L435-436、L464；`write_fluorescence` L212-213）
`run_pipeline` 直接把 `job.outdir` 当输出目录，不校验它是否等于/包含源文件所在目录。`write_fluorescence` 是**流式**读 src 同时 `open(dst,"w")` 写 dst；当 `dst == src` 时第二遍 `open(dst,"w")` 先截断文件，读句柄随后读到 EOF，最终输出 0 字节。实测（60001 行合成数据）：源 `Fluorescence.csv` 2,509,008 字节 → 0 字节，日志仍显示"已写出"。CLI 无任何确认；GUI 只在输出目录已存在"结果文件"时弹"已存在结果文件，是否覆盖？"——用户会以为只是覆盖上次结果，实际毁掉原始数据。`write_events` 因先读后写（L234-235）反而安全，两条路径行为不一致。
修法：resolve 后比较 `os.path.abspath/samefile`，dst 与 src 相同或 dst 在源目录内时拒绝（或强制写到 `源目录/injection_marker/`）；G/U 确认框文案改为明确的"覆盖源文件"警告。

**S2 GUI 异常路径完全失效 → 按钮永久 disabled、无错误提示**（L619-624）
`work()` 的 `except Exception as e: root.after(0, lambda: _fail(e))` 有两层问题：
1. `root.after` 从 worker 线程调用，tkinter 注册 Tcl 命令时检查主线程在 mainloop 中——实测直接抛 `RuntimeError: main thread is not in main loop`（从 `root.after` 抛出，位于 except 块内，未被捕获，线程崩溃）；
2. 即使注册成功，`lambda: _fail(e)` 的 `e` 是 except 局部变量，PEP 3110 规定块结束即删除——回调执行时抛 `NameError: cannot access free variable 'e'`。
实测：把 `run_pipeline` 替换为抛异常后点击"开始分析并写出"，3 秒后按钮仍 `disabled`、日志区为空、无错误框（与"窗口卡死"等价，只能杀进程）。
修法：`except Exception as e: err = e` 立即绑定；用 `queue.Queue` + 主线程 `root.after(100, drain)` 轮询（不要从 worker 直接调 after）；`after` 调用包 try/except 兜底。

**S3 GUI 日志跨线程操作 Tk**（L583-586，被 L619-649 的 worker 线程调用）
`log()` 在 worker 线程直接 `txt.insert` + `txt.see` + `root.update_idletasks()`。Tk 非线程安全，可能崩溃、丢日志或死锁；`update_idletasks` 在非主线程调用尤其危险。
修法：`log()` 只 `q.put(msg)`；主线程 `root.after(100, drain)` 批量写 Text。这与 S2 的修复合并实现最省事。

### 一般

**G1 marker 时间戳与 Fluorescence 行不自洽**（L181 返回 10 Hz 网格 t；L458、L464-466）
Fluorescence 行时间戳 `4747076.639` ms vs 输出 Events.csv `4747100.000` ms，差 **23.361 ms**（End：`4799177.479` vs `4799200.000`，差 22.521 ms）。根因：marker 时间来自 `compute_artifact_index` 的 10 Hz 均匀网格 `tu`，而数据行是原始 100.0025 ms 非整点采样。差异 < 1 个采样间隔，按"最近点"匹配能对上，精确时间戳匹配会失配。
修法：`write_fluorescence` 返回实际命中行的原始 ts（或回读写出行的 ts），用它写 Events.csv。

**G2 候选窗可超出用户搜索窗口**（L181）
`inside` 只按"与窗口相交"筛选；簇合并后直接取首事件 start、末事件 end，未与 `[win_start_s, win_end_s]` 取交集。实测：窗口 30–90 min，事件簇 89.49–91.52 min → 返回 `(5340.0, 5700.0, ...)`，即 **89.00–95.00 min**；端到端合成数据（spike 89.5 min 起 120 s）输出候选窗 89.49–91.52 min、marker 写到窗口外（Events.csv `5369200.000` / `5490900.000`）。
修法：`return (max(best[0][0], win_start_s), min(best[-1][1], win_end_s), ...)`。

**G3 `to_seconds()` 对 1–1000 之间的采样间隔一律当 ms**（L93-102）
判定 `1.0 < dt < 1000.0 → /1000`；秒级数据的 dt=2.0 恰好落入 → 1998 s 变 1.998 s，整段错 1000 倍。真实数据 100 ms 采样正确，但"秒采样"数据会静默错误。
修法：用 span 量级判断（如 `span < 1e4` 更可能为 s），或加 `--time-unit` 显式参数。

**G4 `errors="replace"` 静默破坏非 UTF-8 文件**（L56、L86、L197、L212）
非 UTF-8（如 GBK）的元数据行/事件名被替换为 U+FFFD：实测 GBK 字节 `\xb1\xb8` → 73→74 字节，首行字节不再相同；若 Events 中文名在非 UTF-8 文件里也会被破坏后写回。
修法：先 strict 读，`UnicodeDecodeError` 时回退 gb18030，或直接报错。

**G5 CRLF 源文件被写坏换行**（L225）
marker 行 `fout.write(",".join(f) + "\n")` 强制 LF；未命中行用 `line if line.endswith("\n")` 保留原 `\r\n`。实测 CRLF 60003 行 → 输出 60001 CRLF + 2 个 lone LF，混合换行。
修法：检测源文件首个换行符，写出时统一使用（`rstrip("\r\n") + eol`）。

**G6 两遍扫描对"首字段非数字行"不一致**（L206）
read 静默跳过非数字行，write 第一遍 `float(x)` 无 try → 同一文件 read 能读、write 抛 ValueError。
修法：write 第一遍用与 read 相同的跳过条件（或 try/except）。

**G7 Events.csv 不存在时静默建新文件**（L468、`read_events`）
`read_events` 返回空表 → `write_events` 写出只含 marker 的新文件，用户不知道原始事件丢失。
修法：缺失时告警（GUI 弹提示 / CLI 打 warning）。

**G8 CLI 无 `--start >= --end` 校验**（main，L657-691）
实测 `--start 120 --end 30` → `RuntimeError: 在 120–30 min 内没有检测到超过阈值 3.0 的事件，可降低阈值或放宽时间窗后重试。`——把反的区间原样打印，误导用户去调阈值。GUI 侧有校验（L602-603），CLI 没有。
修法：argparse 解析后 `ap.error("--start 必须小于 --end")`，或 `run_pipeline` 开头校验。

**G9 `min_dur` 用 `b - a` 判定，差一个采样间隔**（L145）
实测 10 个连续点（0.9 s）被拒、11 个点（1.0 s）通过——"最短持续 1.0 s"实际要求 11 个采样点。
修法：`if (e - s) / fs < min_dur` 或 `b - a + dt`。

**G10 `write_events` State 硬编码 1、全 LF**（L246）
源 Event 的 State=0 保留 0，marker 行固定写 1，同一文件 State 语义不一致；CRLF 源被改写为 LF。
修法：State 沿用表头/参数；保留换行风格。

### 建议

- **A1** `MARKER_TOL_SEC = 0.06`（L187）定义后全文未使用——设计意图（marker 吸附容差）落空，超出数据时间范围的 marker 会静默吸附到最近行。
- **A2** `make_figure` 的 `events`、`thr` 参数在函数体内出现 0 次（L262），图里不画事件段也不用阈值，易误导。
- **A3** `read_fluorescence` docstring 写返回 5 项，实际 `return meta, header, ts, ch` 4 项（L52 vs L78）。
- **A4** `run_pipeline` L439 接出 `meta, header` 后从未使用。
- **A5** 静默覆盖已有 Events 字段值（实测 `OldMarker` → `NewMarker`，无警告）。
- **A6** `--outdir` 指向已存在文件 → `FileExistsError [WinError 183]`（实测），消息不友好。
- **A7** `make_figure` 直接调用时单点/全常量 t → `ValueError: cannot convert float NaN to integer`（L274 除零）；run_pipeline 前置 RuntimeError 可挡，直接复用会崩。
- **A8** `load_motion` 不校验 fps 合理性，行为数据时长与荧光不一致时静默错位。
- **A9** `compute_artifact_index` 的 `min_periods=1` 使 5-min 中值去漂移在首尾 5 min 不完整（14 h 数据 + 30–90 min 窗口无影响，短记录有）。
- **A10** `pick_candidate` 假设 events 按时间排序（`cur[-1]`）；实测无序输入返回倒置窗口（89–41 min）。

## 逐条回应审核重点

1. **原始数据只读**：不合格。正常路径不写源文件（run 前后源 md5 一致：`be4e22ad7b01fd9228272f847ab68592`），但 `--outdir` 指向源目录时源文件被覆盖为 0 字节（S1），GUI 确认框文案误导。无只读/同路径校验。
2. **marker 行对齐**：合格。两遍用同一 `split(",",1)[0]` 与同一 k 规则，短行插入 data-line 46000/47000/47500 三处实测 marker 均落在正确行、行数不变；空行不占 k、末尾缺字段行、BOM 均不导致错位。但 CRLF 文件的 marker 行被强制为 LF（G5），非 UTF-8 会破坏元数据字节（G4）。
3. **时间单位**：ms 输入正确（span 52,063,339.659 → 14.46 h），Events.csv 写出为 ms ✓；但 `to_seconds` 对 1<dt<1000 的秒级数据误判（G3）。
4. **算法**：与验证结果一致（79.12–79.99 min、42.0×、52.1 s、9 个事件段），未见致错位的 off-by-one；`detect_events` 的 `b-a` 语义差一个采样间隔（G9），`pick_candidate` 不 clamp 窗口（G2）。
5. **健壮性**：空数据 → `ValueError: Fluorescence.csv 中没读到有效数据行。`；全零/单行数据/窗口内无候选 → `RuntimeError` 带可操作提示（友好）；Events.csv 缺失静默建新文件（G7）；CLI `--start>=--end` 无校验且错误信息误导（G8）；非 UTF-8 静默破坏（G4）。
6. **GUI**：默认路径、控件布局、按钮绑定、中文显示实测正常（截图 `_probe\review_out\gui_idle.png`，880×620）；成功路径实测走通（点击后按钮 disabled → `_done` 弹"完成"框）；线程日志不安全（S3）；异常路径失效（S2）；覆盖确认在 outdir=源目录时文案误导（S1）。
7. **出图**：坐标映射正确（A/B 同 x 轴、C 面板与 B 对齐）；第三面板可选且正常；全零、全 NaN 数据不崩；单点/常量 t 直接调用会崩（A7），正常 CLI/GUI 路径不可达。

## 实测记录（我跑过什么）

- `--nogui` 全量跑真实数据 → 与期望逐条一致（见上表）；与既有 `test_out` md5 相同。
- `--nogui --motion` 全量跑 → 1560×900 三面板图，C 与 B 对齐。
- `--outdir` 指向源目录 → 源文件 0 字节（S1）。
- GUI 截图、GUI 成功路径（patch messagebox）、GUI 异常路径（monkeypatch run_pipeline 抛异常）→ 按钮永久 disabled、无错误框（S2 实测）。
- 边界：空文件/全零/单行/`--start 120 --end 30`/CRLF 源/GBK 源/无表头 Events/Events 缺失/短行插入三位置/BOM 源/单点与常量 t 的 make_figure。
- 源文件未被修改：run 前后 md5 一致。

## 局限

- GUI 在真实显示器上的交互（点击、模态框视觉）只截图验证了 idle 状态；异常路径通过 monkeypatch 复现，与真实点击行为一致。
- 行为数据与荧光的真实同步时长无法从文件校验，仅凭图 C 与 B 对齐判断。
