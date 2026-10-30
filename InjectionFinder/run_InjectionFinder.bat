@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title InjectionFinder

echo ==================================================
echo    InjectionFinder  -  光纤记录注射 marker 自动标注
echo ==================================================
echo.

set "SCRIPT=%~dp0injection_marker_gui.py"
if not exist "%SCRIPT%" (
    echo [错误] 同目录下找不到 injection_marker_gui.py
    echo        请把本 bat 和脚本放在同一个文件夹里再双击。
    echo.
    pause
    exit /b 1
)

rem ---- 逐个候选解释器，找第一个「依赖齐全」的 ----
set "PYEXE="
set "FALLBACK="
for %%C in ("python" "py -3.13" "py -3.12" "py -3.11" "py -3.10" "py -3.9" "py -3.8" "py -3") do (
    if not defined PYEXE call :probe "%%~C"
)

if defined PYEXE goto :launch

echo [检测] 本机 Python 都缺依赖 numpy / pandas / Pillow。
if not defined FALLBACK (
    echo [失败] 没有找到可用的 Python 3，请先安装 Python 3.8 或更高版本：
    echo        https://www.python.org/downloads/windows/
    echo        安装时勾选 "Add python.exe to PATH"，然后重新双击本文件。
    echo.
    pause
    exit /b 1
)

for %%P in (numpy pandas pillow) do (
    echo [安装] %%P 到 !FALLBACK! ... 请稍候
    "!FALLBACK!" -m pip install --upgrade --disable-pip-version-check %%P
)
"!FALLBACK!" -c "import numpy,pandas,PIL,tkinter" >nul 2>nul
if errorlevel 1 (
    echo [重试] 默认源没装全，改用清华镜像再装一次 ...
    "!FALLBACK!" -m pip install --upgrade --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple numpy pandas pillow
    "!FALLBACK!" -c "import numpy,pandas,PIL,tkinter" >nul 2>nul
)
if errorlevel 1 (
    echo.
    echo [失败] 自动安装没能完成。请双击 setup_env.bat 看详细检测结果，
    echo        或把这个窗口里的报错发给维护者。
    echo.
    pause
    exit /b 1
)
set "PYEXE=!FALLBACK!"

:launch
echo [就绪] 解释器：!PYEXE!
echo.
"!PYEXE!" -u "%SCRIPT%"
if errorlevel 1 (
    echo.
    echo [提示] 程序退出码 !errorlevel!，具体错误见上方输出。
    pause
)
exit /b 0

:probe
rem ---- %1 = 候选启动命令，如 python 或 py -3.12 ----
set "CAND=%~1"
%CAND% -c "import sys" >nul 2>nul
if errorlevel 1 exit /b 0
set "CANDEXE="
for /f "delims=" %%p in ('%CAND% -c "import sys;print(sys.executable)" 2^>nul') do set "CANDEXE=%%p"
if not defined CANDEXE exit /b 0
echo "!CANDEXE!" | findstr /i "WindowsApps" >nul && exit /b 0
if not defined FALLBACK set "FALLBACK=!CANDEXE!"
"%CANDEXE%" -c "import numpy,pandas,PIL,tkinter" >nul 2>nul
if errorlevel 1 exit /b 0
set "PYEXE=%CANDEXE%"
exit /b 0