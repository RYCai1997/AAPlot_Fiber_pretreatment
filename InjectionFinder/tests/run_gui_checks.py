# -*- coding: utf-8 -*-
"""GUI 自检：不点鼠标，直接 invoke「开始」按钮并轮询事件循环。

两个场景各跑一个独立进程（Tk 多实例同进程会互相干扰）：
    python tests/run_gui_checks.py 1     # 异常路径：分析抛错
    python tests/run_gui_checks.py 2     # 正常路径：完整跑一遍
    python tests/run_gui_checks.py       # 两个都跑（各起子进程）
"""
import os
import subprocess
import sys
import time

import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import injection_marker_gui as G                                    # noqa: E402

TMP = os.path.join(HERE, "_tmp")
FLUO = os.path.join(TMP, "gui_input", "Fluorescence.csv")
OUT = os.path.join(TMP, "gui_out")
ORIG_PIPELINE = G.run_pipeline
EVENTS = []


def ensure_data():
    """造一份 20 min 的合成记录（窗口设 5–15 min 即可检出）。"""
    if os.path.isfile(FLUO):
        return
    sys.path.insert(0, HERE)
    import numpy as np
    from run_checks import _rows, _write                     # noqa: F401
    os.makedirs(os.path.dirname(FLUO), exist_ok=True)
    ts, ch = _rows()
    _write(FLUO, ts, ch, "Led410Enable:true, Fps:20.0")


def walk(w):
    yield w
    for c in w.winfo_children():
        for x in walk(c):
            yield x


def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def _btn(root):
    for w in walk(root):
        if isinstance(w, ttk.Button) and "开始" in str(w.cget("text")):
            return w
    raise RuntimeError("没找到「开始」按钮")


def scenario(name, fail, expect_error):
    EVENTS.clear()
    if fail:
        def boom(job, log=print):
            raise RuntimeError("模拟失败：输入文件损坏")
        G.run_pipeline = boom
    else:
        G.run_pipeline = ORIG_PIPELINE

    messagebox.showerror = lambda t, m, **k: EVENTS.append(("error", t, m))
    messagebox.askyesno = lambda t, m, **k: (EVENTS.append(("ask", t, m)), False)[1]

    held = {}
    orig_mainloop = tk.Tk.mainloop

    def fake_mainloop(self, n=0):
        root = self
        entries = [w for w in walk(root) if isinstance(w, ttk.Entry)]
        entries[0].delete(0, "end"); entries[0].insert(0, FLUO)
        entries[1].delete(0, "end")                            # Events.csv 留空
        entries[2].delete(0, "end")                            # 行为数据留空
        if not fail:                                           # 合成数据只有 20 min
            entries[3].delete(0, "end"); entries[3].insert(0, "5")
            entries[4].delete(0, "end"); entries[4].insert(0, "15")
        entries[-1].delete(0, "end"); entries[-1].insert(0, OUT)
        b = _btn(root)
        pump(root, 0.4)
        b.invoke()
        held["after_click"] = str(b.cget("state"))
        pump(root, 15.0)
        held["at_end"] = str(b.cget("state"))
        held["logs"] = [w for w in walk(root) if isinstance(w, tk.Text)][0].get("1.0", "end")
        held["events"] = list(EVENTS)
        self.destroy()

    tk.Tk.mainloop = fake_mainloop
    try:
        G.launch_gui()
    finally:
        tk.Tk.mainloop = orig_mainloop
        G.run_pipeline = ORIG_PIPELINE

    ok = held["after_click"] == "disabled" and held["at_end"] == "normal"
    got_err = any(e[0] == "error" for e in held["events"])
    tag = "OK  " if (ok and got_err == expect_error) else "FAIL"
    print("[%s] %s" % (tag, name))
    print("      按钮 点击后=%s 结束时=%s；弹窗=%s" %
          (held["after_click"], held["at_end"], [(e[0], e[1]) for e in held["events"]]))
    tail = [l for l in held["logs"].strip().splitlines() if l.strip()][-3:]
    print("      日志末行：" + " || ".join(tail))


def _one(case):
    ensure_data()
    os.makedirs(OUT, exist_ok=True)
    if case == "1":
        scenario("异常路径：run_pipeline 抛错", True, True)
    else:
        scenario("正常路径：完整跑一遍", False, False)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _one(sys.argv[1])
    else:
        for c in ("1", "2"):
            r = subprocess.run([sys.executable, os.path.abspath(__file__), c],
                               text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                print("[FAIL] 子进程 case %s 退出码 %d" % (c, r.returncode))
