#!/bin/sh
# ============================================================
#  AAPlot Fiber Pretreatment - macOS launcher
#
#   1) find a Python 3.9+ that really has tkinter (Tk needs a real
#      Python build; pip cannot add it)
#   2) create / reuse a private virtualenv for this app
#   3) check requirements.txt through env_check.py
#   4) install whatever is missing with pip, then re-check
#   5) start app.py with that virtualenv's interpreter
#
#  The Windows launcher (run_RWD_fiber_pretreatment.bat) is untouched
#  and still uses the "aaplot" conda environment.  Here conda is only
#  a fallback for the case where no Python with Tk can be found.
#
#  Options :  --no-install / --check   never create/install, start if ready
#             --recreate              rebuild the managed environment
#             --venv | --conda        force one backend
#  Env var :  AAPLOT_NO_PAUSE=1        skip the final pause (automation)
#             AAPLOT_VENV_DIR=<path>  where the virtualenv is created
#             AAPLOT_BACKEND=venv|conda
#
#  Double-clickable: Finder runs it in Terminal.  If Gatekeeper blocks a
#  downloaded copy, use Finder > right click > Open once, or run
#      xattr -d com.apple.quarantine run_RWD_fiber_pretreatment.command
# ============================================================

set -u

ENV_NAME="aaplot"
PREFERRED_PY="3.12"
MIN_MINOR=9
LOG_DIR="$HOME/Library/Logs/AAPlotFiberPretreatment"

# Resolve our own folder.  Works when launched by Finder (relative $0),
# from another working directory, and through paths containing spaces or
# non-ASCII characters.  pwd -P also collapses symlinks.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || {
    printf '[ERROR] Cannot resolve the launcher folder.\n' >&2
    exit 1
}

REQ_FILE="$SCRIPT_DIR/requirements.txt"
CHECK_PY="$SCRIPT_DIR/env_check.py"
APP_PY="$SCRIPT_DIR/app.py"

NO_INSTALL=0
RECREATE=0
BACKEND="${AAPLOT_BACKEND:-venv}"
CONDA=""
BASE_PY=""
PY=""

if [ -n "${HOME:-}" ]; then
    DEFAULT_VENV="$HOME/Library/Application Support/AAPlotFiberPretreatment/venv"
else
    DEFAULT_VENV="$SCRIPT_DIR/.aaplot-venv"
fi
VENV_DIR="${AAPLOT_VENV_DIR:-$DEFAULT_VENV}"

# ---------------------------------------------------------------- helpers

pause_if_needed() {
    # A Finder double-click closes the Terminal window the moment the
    # script ends, so the user would never see an error message.
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
    cat <<'EOF'
Usage: run_RWD_fiber_pretreatment.command [options]

  (no option)    Check the environment, create it when missing, install
                 whatever is missing, then start app.py.
  --no-install   Never create the environment or install packages. Anything
                 already in place is used as it is, so the app still starts;
                 if something is missing the run stops and lists it.
  --check        Alias of --no-install.
  --recreate     Move the managed environment aside and rebuild it.
  --venv         Force the private virtualenv backend (default).
  --conda        Force the conda backend with environment "aaplot".
  -h, --help     Show this help.

Environment variables:
  AAPLOT_NO_PAUSE=1          Do not wait for Return before the window closes.
  AAPLOT_VENV_DIR=<path>     Where the private virtualenv is created.
  AAPLOT_BACKEND=venv|conda  Backend to use instead of the default venv.
EOF
}

# A candidate must be a real interpreter with stdlib Tk, venv and ensurepip.
candidate_ok() {
    [ -x "$1" ] || return 1
    "$1" -c 'import sys
if sys.version_info < (3, 9):
    raise SystemExit(1)
import tkinter, venv, ensurepip' >/dev/null 2>&1
}

version_of() {
    "$1" -c 'import sys; sys.stdout.write("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null
}

arch_of() {
    "$1" -c 'import sys, platform; sys.stdout.write(platform.machine() or "unknown")' 2>/dev/null || true
}

# Ordered by preference.  A bare "python3" comes last on purpose: on a machine
# with a minimal PATH it resolves to Apple's 3.9.6 with the deprecated Tk 8.5,
# and it must not win over a python.org or Homebrew build found by full path.
find_base_python() {
    _probe_list="
        python3.12
        python3.13
        python3.11
        python3.10
        python3.9
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12
        /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11
        /Library/Frameworks/Python.framework/Versions/3.10/bin/python3.10
        /opt/homebrew/bin/python3.13
        /opt/homebrew/bin/python3.12
        /opt/homebrew/bin/python3.11
        /opt/homebrew/bin/python3.10
        /usr/local/bin/python3.13
        /usr/local/bin/python3.12
        /usr/local/bin/python3.11
        /usr/local/bin/python3.10
        /opt/homebrew/bin/python3
        /usr/local/bin/python3
        python3
    "
    for name in $_probe_list; do
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

    # Last resort: the Apple system python (Tk 8.5).  Warn, but a working
    # window beats no window.
    if candidate_ok /usr/bin/python3; then
        printf '%s\n' /usr/bin/python3
        return 0
    fi
    return 1
}

find_conda() {
    if [ -n "${CONDA_EXE:-}" ] && [ -x "$CONDA_EXE" ]; then
        printf '%s\n' "$CONDA_EXE"; return 0
    fi
    _on_path=$(command -v conda 2>/dev/null || true)
    if [ -n "$_on_path" ] && [ -x "$_on_path" ]; then
        printf '%s\n' "$_on_path"; return 0
    fi
    for _candidate in \
        "${HOME:-}/miniconda3/bin/conda" \
        "${HOME:-}/anaconda3/bin/conda" \
        "${HOME:-}/miniforge3/bin/conda" \
        "${HOME:-}/mambaforge/bin/conda" \
        "/opt/homebrew/Caskroom/miniconda/base/bin/conda" \
        "/usr/local/Caskroom/miniconda/base/bin/conda"
    do
        if [ -x "$_candidate" ]; then
            printf '%s\n' "$_candidate"; return 0
        fi
    done
    return 1
}

# Move a broken or outdated environment aside instead of deleting it, so a
# failed upgrade never takes the user's working packages with it.
retire_dir() {
    _target=$1
    _tag=$2
    [ -e "$_target" ] || return 0
    _stamp=$(date '+%Y%m%d-%H%M%S')
    _aside="$_target.$_tag-$_stamp-$$"
    printf '[warn] Moving the previous environment to:\n       %s\n' "$_aside"
    mv -- "$_target" "$_aside" || {
        printf '[ERROR] Cannot move %s aside.\n' "$_target" >&2
        return 1
    }
    return 0
}

venv_is_usable() {
    [ -x "$VENV_DIR/bin/python" ] || return 1
    "$VENV_DIR/bin/python" -c 'import sys' >/dev/null 2>&1 || return 1
    return 0
}

create_venv() {
    printf '[1/3] Creating the private virtualenv...\n'
    mkdir -p -- "$(dirname -- "$VENV_DIR")" || {
        printf '[ERROR] Cannot create %s\n' "$(dirname -- "$VENV_DIR")" >&2
        return 1
    }
    if ! "$BASE_PY" -m venv "$VENV_DIR"; then
        # Some stripped Python builds ship without pip; retry explicitly.
        printf '[warn] venv creation failed, retrying with --without-pip...\n'
        if ! "$BASE_PY" -m venv --without-pip "$VENV_DIR"; then
            printf '[ERROR] Cannot create a virtualenv at:\n        %s\n' "$VENV_DIR" >&2
            return 1
        fi
        if ! "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1; then
            printf '[ERROR] No pip inside the new virtualenv and ensurepip failed.\n' >&2
            return 1
        fi
    fi
    printf '%s %s\n' "$BASE_PY" "$(version_of "$BASE_PY")" > "$VENV_DIR/.aaplot-base-python" 2>/dev/null || true
    return 0
}

# ---------------------------------------------------------------- preflight

for arg in "$@"; do
    case "$arg" in
        --no-install|--check) NO_INSTALL=1 ;;
        --recreate) RECREATE=1 ;;
        --venv) BACKEND="venv" ;;
        --conda) BACKEND="conda" ;;
        -h|--help) usage; finish 0 ;;
        *)
            printf '[ERROR] Unknown option: %s\n' "$arg" >&2
            usage >&2
            finish 2
            ;;
    esac
done

if [ ! -f "$REQ_FILE" ] || [ ! -f "$CHECK_PY" ] || [ ! -f "$APP_PY" ]; then
    printf '[ERROR] Launcher files are incomplete in:\n        %s\n' "$SCRIPT_DIR" >&2
    printf '        requirements.txt, env_check.py and app.py must sit next to this script.\n' >&2
    finish 1
fi

printf '%s\n' '============================================================'
printf ' %s - macOS environment check\n' 'AAPlot Fiber Pretreatment'
printf ' folder: %s\n' "$SCRIPT_DIR"
printf ' macOS : %s (%s)\n' "$(sw_vers -productVersion 2>/dev/null || echo unknown)" "$(uname -m)"
printf '%s\n' '============================================================'

# ---------------------------------------------------------------- backend

# Default backend is a private virtualenv.  Only fall back to conda when no
# Python with Tk exists, because pip cannot supply tkinter.
if [ "$BACKEND" = "venv" ]; then
    BASE_PY=$(find_base_python || true)
    if [ -z "$BASE_PY" ]; then
        CONDA=$(find_conda || true)
        if [ -n "$CONDA" ]; then
            BACKEND="conda"
            printf '[warn] No Python 3.9+ with Tk was found; using conda instead.\n'
            printf '       (pass --venv to require the virtualenv backend anyway)\n'
        fi
    fi
fi

if [ "$BACKEND" = "conda" ]; then
    CONDA=$(find_conda || true)
    if [ -z "$CONDA" ]; then
        printf '[ERROR] --conda was given but conda is not installed.\n' >&2
        printf '        Drop the flag to use the private virtualenv instead.\n' >&2
        finish 1
    fi
    printf '[env] backend : conda (%s)\n' "$CONDA"
    printf '[env] env name: %s\n' "$ENV_NAME"

    ENV_EXISTS=0
    if "$CONDA" run -n "$ENV_NAME" python -c 'import sys' >/dev/null 2>&1; then
        ENV_EXISTS=1
    fi

    if [ "$ENV_EXISTS" -eq 1 ] && [ "$RECREATE" -eq 1 ] && [ "$NO_INSTALL" -eq 0 ]; then
        printf '[1/3] Removing conda environment "%s" because --recreate was given...\n' "$ENV_NAME"
        if ! "$CONDA" remove -n "$ENV_NAME" --all -y; then
            printf '[ERROR] Failed to remove conda environment "%s".\n' "$ENV_NAME" >&2
            finish 1
        fi
        ENV_EXISTS=0
    fi

    if [ "$ENV_EXISTS" -eq 0 ]; then
        if [ "$NO_INSTALL" -eq 1 ]; then
            printf '[MISSING] conda environment "%s" does not exist.\n' "$ENV_NAME" >&2
            printf '          Create it with: "%s" create -n %s python=%s -y\n' "$CONDA" "$ENV_NAME" "$PREFERRED_PY" >&2
            finish 1
        fi
        printf '[1/3] Creating conda environment "%s" with Python %s...\n' "$ENV_NAME" "$PREFERRED_PY"
        if ! "$CONDA" create -n "$ENV_NAME" "python=$PREFERRED_PY" -y; then
            printf '[ERROR] Failed to create conda environment "%s".\n' "$ENV_NAME" >&2
            finish 1
        fi
    fi

    # conda run may print warnings on stdout, so take the last real line.
    _probe=$(mktemp "${TMPDIR:-/tmp}/aaplot-py.XXXXXX")
    if "$CONDA" run -n "$ENV_NAME" python -c 'import sys; print(sys.executable)' > "$_probe" 2>/dev/null; then
        PY=$(grep -v '^[[:space:]]*$' "$_probe" | tail -n 1)
    else
        PY=""
    fi
    rm -f -- "$_probe"
    if [ -z "$PY" ] || [ ! -x "$PY" ]; then
        printf '[ERROR] Cannot locate Python inside conda environment "%s".\n' "$ENV_NAME" >&2
        finish 1
    fi
else
    BACKEND="venv"
    if [ -z "$BASE_PY" ]; then
        cat >&2 <<'EOF'
[ERROR] No Python 3.9+ with tkinter, venv and pip was found.
        tkinter ships with CPython and cannot be installed with pip, so the
        launcher needs a Python build that already includes Tk.
        Fix with one of:
            brew install python@3.12 python-tk@3.12
            install the universal2 installer from https://www.python.org/downloads/
            install Miniforge from https://github.com/conda-forge/miniforge
EOF
        finish 1
    fi

    printf '[env] backend      : virtualenv\n'
    printf '[env] base Python  : %s (%s, %s)\n' "$BASE_PY" "$(version_of "$BASE_PY")" "$(arch_of "$BASE_PY")"
    printf '[env] virtualenv   : %s\n' "$VENV_DIR"

    case "$BASE_PY" in
        /usr/bin/python3)
            printf '[warn] Using the Apple system Python. Its Tk 8.5 is deprecated;\n'
            printf '       install python.org Python or Miniforge if the window misbehaves.\n'
            ;;
    esac

    _host_arch=$(uname -m)
    _py_arch=$(arch_of "$BASE_PY")
    if [ -n "$_py_arch" ] && [ -n "$_host_arch" ] && [ "$_py_arch" != "$_host_arch" ]; then
        printf '[warn] Python is %s but this Mac is %s (Rosetta). Native wheels will be\n' "$_py_arch" "$_host_arch"
        printf '       installed for %s; that works, but a native build starts faster.\n' "$_py_arch"
    fi

    if [ "$RECREATE" -eq 1 ] && [ "$NO_INSTALL" -eq 0 ] && [ -e "$VENV_DIR" ]; then
        printf '[1/3] --recreate given; rebuilding the environment.\n'
        retire_dir "$VENV_DIR" "old" || finish 1
    fi

    if [ ! -e "$VENV_DIR" ]; then
        if [ "$NO_INSTALL" -eq 1 ]; then
            printf '[MISSING] The private virtualenv does not exist:\n          %s\n' "$VENV_DIR" >&2
            printf '          Run this launcher once without --no-install.\n' >&2
            finish 1
        fi
        create_venv || finish 1
    elif ! venv_is_usable; then
        printf '[warn] The existing virtualenv is broken (base Python moved or upgraded).\n'
        if [ "$NO_INSTALL" -eq 1 ]; then
            printf '[MISSING] Rebuild it by running without --no-install, or with --recreate.\n' >&2
            finish 1
        fi
        retire_dir "$VENV_DIR" "broken" || finish 1
        create_venv || finish 1
    else
        printf '[1/3] Reusing the existing virtualenv.\n'
    fi

    PY="$VENV_DIR/bin/python"
    # An `uv`-created or migrated environment can lack pip.
    if ! "$PY" -m pip --version >/dev/null 2>&1; then
        printf '[warn] pip is missing in the virtualenv; bootstrapping it...\n'
        if [ "$NO_INSTALL" -eq 0 ]; then
            "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || {
                printf '[ERROR] Cannot install pip inside the virtualenv.\n' >&2
                finish 1
            }
        fi
    fi
fi

printf '[env] interpreter  : %s (%s)\n' "$PY" "$(version_of "$PY")"

# ------------------------------------------------------- dependency check

# Homebrew / python.org builds on macOS sometimes ship a CA bundle that
# certificate verification cannot find.  Point OpenSSL at the system store
# only when the environment does not already define one.
if [ -z "${SSL_CERT_FILE:-}" ] && [ -r /etc/ssl/cert.pem ]; then
    SSL_CERT_FILE=/etc/ssl/cert.pem
    export SSL_CERT_FILE
fi

printf '[2/3] Checking dependencies against requirements.txt...\n'
if ! "$PY" "$CHECK_PY" "$REQ_FILE"; then
    if [ "$NO_INSTALL" -eq 1 ]; then
        printf '[MISSING] --no-install was given, so nothing was installed.\n' >&2
        printf '          Fix with: "%s" -m pip install -r "%s"\n' "$PY" "$REQ_FILE" >&2
        finish 1
    fi

    printf '[3/3] Installing requirements.txt with pip...\n'
    if ! "$PY" -m pip install --disable-pip-version-check --timeout 60 --retries 5 -r "$REQ_FILE"; then
        if [ "$BACKEND" = "conda" ]; then
            printf '[warn] pip failed; retrying the same packages with conda...\n'
            if ! "$CONDA" install -n "$ENV_NAME" -y matplotlib numpy pandas scipy; then
                printf '[ERROR] Both pip and conda failed to install the dependencies.\n' >&2
                finish 1
            fi
        else
            printf '[ERROR] pip could not install the dependencies.\n' >&2
            printf '        Retry by hand to see the full error:\n' >&2
            printf '            "%s" -m pip install -r "%s"\n' "$PY" "$REQ_FILE" >&2
            printf '        Behind a slow or filtered network, point pip at a mirror first:\n' >&2
            printf '            export PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/\n' >&2
            finish 1
        fi
    fi

    printf 'Re-checking dependencies...\n'
    if ! "$PY" "$CHECK_PY" "$REQ_FILE"; then
        printf '[ERROR] Dependencies are still incomplete after installation.\n' >&2
        printf '        Rebuild the environment with: %s --recreate\n' "$0" >&2
        finish 1
    fi
else
    printf '[3/3] Nothing to install.\n'
fi

# ------------------------------------------------------ runtime preflight

# tkinter cannot be pip-installed, so verify it inside the *managed*
# environment too, and import the local processing module the way app.py
# will.  A failure here is clearer than a traceback from the GUI.
printf 'Checking tkinter, the Matplotlib Tk backend and the local modules...\n'
if ! "$PY" -c 'import sys
sys.path.insert(0, sys.argv[1])
import tkinter
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.backends.backend_tkagg
import numpy, pandas, scipy
import processing' "$SCRIPT_DIR"; then
    if [ "$BACKEND" = "conda" ] && [ "$NO_INSTALL" -eq 0 ]; then
        printf '[warn] Tk support looks broken; trying conda install tk...\n'
        "$CONDA" install -n "$ENV_NAME" -y tk || true
    fi
fi

if ! "$PY" -c 'import sys
sys.path.insert(0, sys.argv[1])
import tkinter
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.backends.backend_tkagg
import numpy, pandas, scipy
import processing' "$SCRIPT_DIR"; then
    cat >&2 <<EOF
[ERROR] The Python packages are installed but the GUI cannot start.
        tkinter / Tk is not usable in this interpreter, and pip cannot
        install it.  Use a Python that includes Tk:
            brew install python@3.12 python-tk@3.12
            or the installer from https://www.python.org/downloads/
            or Miniforge, then rerun with --conda
        Interpreter: $PY
EOF
    finish 1
fi

# ---------------------------------------------------------------- launch

printf '\n[OK] Starting app.py with the managed %s environment...\n' "$BACKEND"
printf '     Error log: %s/aaplot_errors.log\n\n' "$LOG_DIR"

# Run from the repository folder so relative paths and `import processing`
# resolve the same way as on Windows (the .bat does `cd /d "%~dp0"`).
cd -- "$SCRIPT_DIR" || {
    printf '[ERROR] Cannot enter %s\n' "$SCRIPT_DIR" >&2
    finish 1
}

"$PY" "$APP_PY"
APP_RC=$?
if [ "$APP_RC" -ne 0 ]; then
    # Closing the window returns 0.  A signal such as SIGTERM (closing the
    # Terminal window, or "Force Quit") ends the Tk bundle with 1 and leaves
    # no traceback, so only mention the log when it actually has content.
    printf '\n[warn] app.py exited with code %s\n' "$APP_RC" >&2
    if [ -s "$LOG_DIR/aaplot_errors.log" ]; then
        printf '       The last traceback was appended to:\n       %s/aaplot_errors.log\n' "$LOG_DIR" >&2
    else
        printf '       No traceback was recorded; a forced quit looks like this too.\n' >&2
    fi
fi
finish "$APP_RC"
