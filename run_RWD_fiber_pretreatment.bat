@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
rem Keep this file pure ASCII. UTF-8 is only used for child-process output
rem (env_check.py forces UTF-8 on its own streams).
cd /d "%~dp0"

rem ============================================================
rem  AAPlot Fiber Pretreatment launcher
rem    1) locate Anaconda
rem    2) ensure conda env "%ENV_NAME%"
rem    3) check requirements.txt (env_check.py)
rem    4) install whatever is missing (conda, pip fallback)
rem    5) start app.py with that env's python
rem
rem  Options :  --no-install        only check, never create env / install
rem             --recreate          delete the env and rebuild it from scratch
rem  Env var :  AAPLOT_NO_PAUSE=1   skip the final pause (automation)
rem
rem  Packages are installed with pip from requirements.txt inside the conda
rem  env (the same versions Anaconda's own env uses work best on Windows).
rem ============================================================

set "ENV_NAME=aaplot"
set "PY_VER=3.12"
set "REQ_FILE=%~dp0requirements.txt"
set "CHECK_PY=%~dp0env_check.py"
set "NO_INSTALL="
set "RECREATE="
set "CHECK_LOG="
if /i "%~1"=="--no-install" set "NO_INSTALL=1"
if /i "%~1"=="--recreate" set "RECREATE=1"

echo ============================================================
echo  AAPlot Fiber Pretreatment - environment check
echo  conda env: %ENV_NAME%    python: %PY_VER%
echo ============================================================

rem ---------- 1) locate Anaconda root ----------
set "CONDA_ROOT="

if defined CONDA_EXE (
  for %%A in ("%CONDA_EXE%\..\..") do if exist "%%~fA\Scripts\conda.exe" set "CONDA_ROOT=%%~fA"
)

for %%R in (
  "C:\ProgramData\Anaconda3"
  "C:\Anaconda3"
  "%USERPROFILE%\anaconda3"
  "%USERPROFILE%\miniconda3"
  "%LOCALAPPDATA%\anaconda3"
  "%LOCALAPPDATA%\miniconda3"
  "D:\Anaconda3"
  "E:\Anaconda3"
) do if not defined CONDA_ROOT if exist "%%~R\Scripts\conda.exe" set "CONDA_ROOT=%%~R"

if not defined CONDA_ROOT (
  for /f "delims=" %%C in ('where conda 2^>nul') do if not defined CONDA_ROOT for %%A in ("%%C\..\..") do if exist "%%~fA\Scripts\conda.exe" set "CONDA_ROOT=%%~fA"
)

if not defined CONDA_ROOT goto :no_conda

set "CONDA=%CONDA_ROOT%\Scripts\conda.exe"
echo [env] Anaconda root : %CONDA_ROOT%

rem ---------- 2) ensure the conda environment exists ----------
rem NOTE: never put a quoted exe path at the start of a for /f command string,
rem       cmd strips the outer quotes and the command breaks.
set "ENV_LIST=%TEMP%\aaplot_envlist_%RANDOM%.txt"
"%CONDA%" env list > "%ENV_LIST%" 2>nul
set "ENV_EXISTS="
findstr /R /C:"^%ENV_NAME% " "%ENV_LIST%" >nul 2>&1 && set "ENV_EXISTS=1"
if exist "%ENV_LIST%" del /q "%ENV_LIST%" >nul 2>&1

if defined ENV_EXISTS if defined RECREATE if not defined NO_INSTALL (
  echo [1/3] Removing conda env "%ENV_NAME%" because --recreate was given ...
  "%CONDA%" remove -n %ENV_NAME% --all -y
  set "ENV_EXISTS="
)

if defined ENV_EXISTS goto :env_ready

if defined NO_INSTALL (
  echo [MISSING] conda env "%ENV_NAME%" does not exist.
  echo           Create it manually with:
  echo               "%CONDA%" create -n %ENV_NAME% python=%PY_VER% -y
  echo               "%CONDA%" run -n %ENV_NAME% python -m pip install -r "%REQ_FILE%"
  goto :fail
)

echo [1/3] Creating conda env "%ENV_NAME%" with python %PY_VER%, this may take a few minutes ...
"%CONDA%" create -n %ENV_NAME% python=%PY_VER% -y
if errorlevel 1 goto :create_failed
:env_ready

rem ---------- 3) resolve the env python ----------
set "PY=%CONDA_ROOT%\envs\%ENV_NAME%\python.exe"
if exist "%PY%" goto :have_py

set "PY="
set "PY_PROBE=%TEMP%\aaplot_py_%RANDOM%.txt"
"%CONDA%" run -n %ENV_NAME% python -c "import sys;sys.stdout.write(sys.executable+chr(10))" > "%PY_PROBE%" 2>nul
if exist "%PY_PROBE%" set /p PY=<"%PY_PROBE%"
if exist "%PY_PROBE%" del /q "%PY_PROBE%" >nul 2>&1
if not defined PY (
  echo [ERROR] Cannot locate python.exe inside conda env "%ENV_NAME%".
  echo         The env may live outside %CONDA_ROOT%\envs.
  goto :fail
)
:have_py
echo [env] python        : %PY%
rem If the env was set up in an older run and the app crashes on startup,
rem rerun this script with --recreate to rebuild the env cleanly.

rem ---------- 4) check requirements ----------
echo [2/3] Checking dependencies against requirements.txt ...
set "CHECK_LOG=%TEMP%\aaplot_env_check_%RANDOM%.log"
"%PY%" "%CHECK_PY%" > "%CHECK_LOG%" 2>&1
set "CHECK_RC=%ERRORLEVEL%"
type "%CHECK_LOG%"
if "%CHECK_RC%"=="0" goto :deps_ok

set "MISSING_LIST="
for /f "tokens=1,* delims=:" %%A in ('findstr /B /C:"PACKAGES_TO_INSTALL:" "%CHECK_LOG%"') do set "MISSING_LIST=%%B"

if not defined MISSING_LIST (
  echo [ERROR] Dependency check failed without a package list, see the output above.
  goto :fail
)

if defined NO_INSTALL (
  echo [MISSING] --no-install given, nothing was installed. Fix manually with:
  echo               "%PY%" -m pip install -r "%REQ_FILE%"
  goto :fail
)

rem ---------- 5) install what is missing ----------
echo [3/3] Installing requirements.txt with pip into the conda env ...
"%PY%" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
  echo [WARN] pip install failed, falling back to conda: !MISSING_LIST!
  "%CONDA%" install -n %ENV_NAME% -y !MISSING_LIST!
  if errorlevel 1 goto :install_failed
)

echo Re-checking dependencies ...
"%PY%" "%CHECK_PY%"
if errorlevel 1 goto :still_missing
:deps_ok

rem ---------- 6) start the app ----------
echo.
echo [OK] Starting app.py with the conda env python ...
"%PY%" "%~dp0app.py"
set "APP_RC=%ERRORLEVEL%"
if not "%APP_RC%"=="0" echo [WARN] app.py exited with code %APP_RC%
goto :done


:no_conda
echo [ERROR] Anaconda / conda was not found.
echo         Install Anaconda, or make sure conda.exe exists at:
echo             C:\ProgramData\Anaconda3\Scripts\conda.exe
echo         Then set up the env manually:
echo             conda create -n %ENV_NAME% python=%PY_VER% -y
echo             conda run -n %ENV_NAME% python -m pip install -r "%REQ_FILE%"
goto :fail

:create_failed
echo [ERROR] Failed to create conda env "%ENV_NAME%".
echo         Reproduce manually to see the reason:
echo             "%CONDA%" create -n %ENV_NAME% python=%PY_VER% -y
goto :fail

:install_failed
echo [ERROR] Failed to install the missing packages, see the output above.
echo         Install manually:
echo             "%PY%" -m pip install -r "%REQ_FILE%"
goto :fail

:still_missing
echo [ERROR] Dependencies are still incomplete after installation.
echo         Fix manually:
echo             "%PY%" -m pip install -r "%REQ_FILE%"
echo         Or rebuild the whole env:
echo             run_RWD_fiber_pretreatment.bat --recreate
goto :fail

:done
if defined CHECK_LOG if exist "%CHECK_LOG%" del /q "%CHECK_LOG%" >nul 2>&1
echo.
if not defined AAPLOT_NO_PAUSE pause
endlocal
exit /b 0

:fail
if defined CHECK_LOG if exist "%CHECK_LOG%" del /q "%CHECK_LOG%" >nul 2>&1
echo.
if not defined AAPLOT_NO_PAUSE pause
endlocal
exit /b 1
