#!/bin/sh
# ============================================================
#  InperFormatTransfer - macOS launcher
#
#   1) find a Python 3.9+ that really has tkinter
#   2) reuse the virtualenv shared with the main pretreatment app,
#      creating it when it does not exist yet
#   3) start inper_transfer_gui.py
#
#  This tool uses only the standard library (sqlite3, tkinter), so it has
#  no requirements.txt and nothing is installed unless the environment has
#  to be created.  The Windows launcher (run_inper_transfer.bat) is unchanged.
#
#  Options :  --no-install   check only, never create the environment
#             --recreate     rebuild the shared virtualenv
#  Env var :  AAPLOT_NO_PAUSE=1        skip the final pause
#             AAPLOT_VENV_DIR=<path>  where the virtualenv lives
# ============================================================

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || {
    printf '[ERROR] Cannot resolve the launcher folder.\n' >&2
    exit 1
}
ENTRY="$SCRIPT_DIR/inper_transfer_gui.py"

if [ -n "${HOME:-}" ]; then
    DEFAULT_VENV="$HOME/Library/Application Support/AAPlotFiberPretreatment/venv"
else
    DEFAULT_VENV="$SCRIPT_DIR/.aaplot-venv"
fi
VENV_DIR="${AAPLOT_VENV_DIR:-$DEFAULT_VENV}"

NO_INSTALL=0
RECREATE=0

pause_if_needed() {
    if [ "${AAPLOT_NO_PAUSE:-0}" != "1" ] && [ -t 0 ]; then
        printf '\nPress Return to close this window...'
        IFS= read -r _answer || true
    fi
}

finish() {
    _code=$1
    pause_if_needed
    exit "$_code"
}

usage() {
    printf 'Usage: %s [--no-install] [--recreate] [--] [script arguments...]\n' "$(basename -- "$0")"
    printf '  --no-install   Check only; never create the environment.\n'
    printf '  --recreate     Rebuild the shared virtualenv.\n'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-install|--check) NO_INSTALL=1; shift ;;
        --recreate) RECREATE=1; shift ;;
        -h|--launcher-help) usage; finish 0 ;;
        --) shift; break ;;
        *) break ;;
    esac
done

candidate_ok() {
    [ -x "$1" ] || return 1
    "$1" -c 'import sys
if sys.version_info < (3, 9):
    raise SystemExit(1)
import tkinter, venv' >/dev/null 2>&1
}

# A bare "python3" comes last: it is Apple's 3.9 with the deprecated Tk 8.5
# when PATH is minimal, and must not win over a python.org or Homebrew build.
find_base_python() {
    for name in \
        python3.12 python3.13 python3.11 python3.10 python3.9 \
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
        /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 \
        /opt/homebrew/bin/python3.13 \
        /opt/homebrew/bin/python3.12 \
        /opt/homebrew/bin/python3.11 \
        /usr/local/bin/python3.13 \
        /usr/local/bin/python3.12 \
        /usr/local/bin/python3.11 \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        python3 \
        /usr/bin/python3
    do
        case "$name" in
            /*) cand=$name ;;
            *)  cand=$(command -v "$name" 2>/dev/null || true) ;;
        esac
        [ -n "$cand" ] || continue
        if candidate_ok "$cand"; then
            printf '%s\n' "$cand"
            return 0
        fi
    done
    return 1
}

if [ ! -f "$ENTRY" ]; then
    printf '[ERROR] inper_transfer_gui.py is missing next to this launcher:\n        %s\n' "$SCRIPT_DIR" >&2
    finish 1
fi

printf '%s\n' '============================================================'
printf '%s\n' ' InperFormatTransfer - macOS launcher'
printf ' folder: %s\n' "$SCRIPT_DIR"
printf '%s\n' '============================================================'

if [ -x "$VENV_DIR/bin/python" ] && "$VENV_DIR/bin/python" -c 'import sys' >/dev/null 2>&1; then
    if [ "$RECREATE" -eq 1 ] && [ "$NO_INSTALL" -eq 0 ]; then
        _aside="$VENV_DIR.old-$(date '+%Y%m%d-%H%M%S')-$$"
        printf '[1/2] --recreate given; moving the environment to:\n      %s\n' "$_aside"
        mv -- "$VENV_DIR" "$_aside" || { printf '[ERROR] Cannot move the environment aside.\n' >&2; finish 1; }
    else
        printf '[1/2] Reusing the virtualenv: %s\n' "$VENV_DIR"
    fi
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
    if [ "$NO_INSTALL" -eq 1 ]; then
        printf '[MISSING] No virtualenv at:\n          %s\n' "$VENV_DIR" >&2
        printf '          Run this launcher once without --no-install.\n' >&2
        finish 1
    fi
    BASE_PY=$(find_base_python || true)
    if [ -z "$BASE_PY" ]; then
        cat >&2 <<'EOF'
[ERROR] No Python 3.9+ with tkinter was found.
        tkinter ships with CPython and pip cannot install it.
        Fix with:  brew install python@3.12 python-tk@3.12
        or install Python from https://www.python.org/downloads/
EOF
        finish 1
    fi
    printf '[1/2] Creating the virtualenv with %s ...\n' "$BASE_PY"
    mkdir -p -- "$(dirname -- "$VENV_DIR")" || { printf '[ERROR] Cannot create the folder.\n' >&2; finish 1; }
    if ! "$BASE_PY" -m venv "$VENV_DIR"; then
        printf '[ERROR] Cannot create a virtualenv at:\n        %s\n' "$VENV_DIR" >&2
        finish 1
    fi
fi

PY="$VENV_DIR/bin/python"
printf '[env] Python: %s\n' "$("$PY" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null)"

# tkinter is the only dependency, but it lives outside site-packages, so a
# broken base Python would only show up here.
printf '[2/2] Checking tkinter and the local modules...\n'
if ! "$PY" -c 'import sys
sys.path.insert(0, sys.argv[1])
import tkinter
import inper_db, inper_transfer' "$SCRIPT_DIR"; then
    printf '[ERROR] tkinter or the local modules cannot be imported with:\n        %s\n' "$PY" >&2
    printf '        Rebuild with --recreate, or install Python from python.org.\n' >&2
    finish 1
fi

# --no-install means "check, do not install": with every check passed there is
# nothing left to do, so stop rather than opening the window anyway.
if [ "$NO_INSTALL" -eq 1 ]; then
    printf '\n[check] Everything above passed; %s was not started (--no-install).\n' "$(basename -- "$ENTRY")"
    finish 0
fi

printf '\n[OK] Starting inper_transfer_gui.py ...\n\n'
cd -- "$SCRIPT_DIR" || { printf '[ERROR] Cannot enter %s\n' "$SCRIPT_DIR" >&2; finish 1; }

"$PY" "$ENTRY" "$@"
APP_RC=$?
if [ "$APP_RC" -ne 0 ]; then
    printf '\n[warn] inper_transfer_gui.py exited with code %s\n' "$APP_RC" >&2
fi
finish "$APP_RC"
