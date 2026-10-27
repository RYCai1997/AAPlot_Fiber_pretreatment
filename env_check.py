"""校验当前 Python 环境是否满足 requirements.txt。

用法:   python env_check.py [requirements.txt 的路径]
退出码: 0 = 全部满足; 2 = 有缺失或版本过低; 3 = 找不到 requirements.txt

最后几行机器可读输出，供 run_RWD_fiber_pretreatment.bat 解析:
    PACKAGES_TO_INSTALL: <包名...>    仅在有问题时输出
    RESULT: OK | MISSING <包名...> | ERROR
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

SPEC_OPS = (">=", "<=", "==", "!=", "~=", ">", "<")
REQ_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?P<spec>.*)$")


def parse_version(text: str) -> tuple:
    """把版本号切成 ((0, 数字) | (1, 字符串)) 序列，足以比较常规版本号。"""
    parts = []
    for chunk in re.split(r"[._\-+]", str(text).strip()):
        m = re.match(r"^(\d+)(.*)$", chunk)
        if m:
            parts.append((0, int(m.group(1))))
            if m.group(2):
                parts.append((1, m.group(2)))
        elif chunk:
            parts.append((1, chunk))
    return tuple(parts)


def cmp_version(a: str, b: str) -> int:
    pa, pb = parse_version(a), parse_version(b)
    n = max(len(pa), len(pb))
    pad = (0, 0)
    pa = pa + (pad,) * (n - len(pa))
    pb = pb + (pad,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


def satisfies(installed: str, op: str, required: str) -> bool:
    c = cmp_version(installed, required)
    if op == ">=":
        return c >= 0
    if op == ">":
        return c > 0
    if op == "<=":
        return c <= 0
    if op == "<":
        return c < 0
    if op == "==":
        return c == 0
    if op == "!=":
        return c != 0
    if op == "~=":
        return c >= 0  # 本清单只用 >=，兼容版本段不做精确处理
    return True


def parse_requirements(path: Path) -> list:
    items = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = REQ_RE.match(line)
        if not m:
            continue
        specs = []
        for piece in m.group("spec").split(","):
            piece = piece.strip()
            for op in SPEC_OPS:
                if piece.startswith(op):
                    specs.append((op, piece[len(op):].strip()))
                    break
        items.append((m.group("name"), specs))
    return items


def main(argv: list) -> int:
    # 统一 UTF-8 输出：脚本里 chcp 65001，控制台 / 重定向 / 管道 都不会乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    req = Path(argv[1]) if len(argv) > 1 else Path(__file__).with_name("requirements.txt")
    print(f"Python   : {sys.version.split()[0]}  ({sys.executable})")
    if not req.is_file():
        print(f"[错误] 找不到依赖清单: {req}")
        print("RESULT: ERROR")
        return 3
    print(f"依赖清单 : {req}")
    print("-" * 68)

    bad = []
    for name, specs in parse_requirements(req):
        need = ", ".join(f"{op}{ver}" for op, ver in specs) or "任意版本"
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = None
        if installed is None:
            print(f"[缺失] {name:<14} 要求 {need}")
            bad.append(name)
            continue
        failed = [(op, ver) for op, ver in specs if not satisfies(installed, op, ver)]
        if failed:
            need_bad = ", ".join(f"{op}{ver}" for op, ver in failed)
            print(f"[过低] {name:<14} 要求 {need_bad:<12} 当前 {installed}")
            bad.append(name)
        else:
            print(f"[通过] {name:<14} {installed:<12} ({need})")

    print("-" * 68)
    if bad:
        pkgs = list(dict.fromkeys(bad))
        print(f"共 {len(bad)} 项不满足，需要安装/升级: {' '.join(pkgs)}")
        print(f"PACKAGES_TO_INSTALL: {' '.join(pkgs)}")
        print(f"RESULT: MISSING {' '.join(pkgs)}")
        return 2
    print("全部依赖已满足。")
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
