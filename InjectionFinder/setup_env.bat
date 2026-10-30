@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title InjectionFinder - 环境检测与安装

echo ==================================================
echo    InjectionFinder  环境检测
echo ==================================================
echo.

set "ANYEXE="
set "READYEXE="
for %%C in ("python" "py -3.13" "py -3.12" "py -3.11" "py -3.10" "py -3.9" "py -3.8" "py -3") do call :check "%%~C"

echo.
if defined READYEXE (
    echo [结论] 环境已就绪：!READYEXE!
    echo        直接双击 run_InjectionFinder.bat 即可启动界面。
    echo.
    pause
    exit /b 0
)
if not defined ANYEXE (
    echo [结论] 这台电脑没有可用的 Python 3。
    echo        请安装 Python 3.8+：https://www.python.org/downloads/windows/
    echo        安装时勾选 "Add python.exe to PATH"，然后重新运行本文件。
    echo.
    pause
    exit /b 1
)

echo [结论] Python 可用，但缺少第三方库，开始安装到：
echo        !ANYEXE!
echo.
for %%P in (numpy pandas pillow) do (
    echo   - 安装 %%P ...
    "!ANYEXE!" -m pip install --upgrade --disable-pip-version-check %%P
)
"!ANYEXE!" -c "import numpy,pandas,PIL,tkinter" >nul 2>nul
if errorlevel 1 (
    echo   - 默认源没装全，改用清华镜像重试 ...
    "!ANYEXE!" -m pip install --upgrade --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple numpy pandas pillow
)
"!ANYEXE!" -c "import numpy,pandas,PIL,tkinter" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [失败] 依赖仍不完整。可手动执行：
    echo        "!ANYEXE!" -m pip install numpy pandas pillow
    echo.
    pause
    exit /b 1
)
echo.
echo [完成] 环境已就绪，双击 run_InjectionFinder.bat 启动。
echo.
pause
exit /b 0

:check
rem ---- %1 = 候选启动命令 ----
set "CAND=%~1"
%CAND% -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo   [--] %~1 : 未安装
    exit /b 0
)
set "VER="
for /f "delims=" %%p in ('%CAND% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "VER=%%p"
set "EXEF="
for /f "delims=" %%p in ('%CAND% -c "import sys;print(sys.executable)" 2^>nul') do set "EXEF=%%p"
if not defined EXEF exit /b 0
echo "!EXEF!" | findstr /i "WindowsApps" >nul && exit /b 0
if not defined ANYEXE set "ANYEXE=!EXEF!"
set "LINE=  [%~1] Python !VER!  !EXEF!"
for %%M in (numpy pandas PIL tkinter) do (
    "!EXEF!" -c "import %%M" >nul 2>nul
    if errorlevel 1 ( set "LINE=!LINE!  %%~M=缺" ) else ( set "LINE=!LINE!  %%~M=OK" )
)
echo(!LINE!
"%EXEF%" -c "import numpy,pandas,PIL,tkinter" >nul 2>nul
if not errorlevel 1 if not defined READYEXE set "READYEXE=!EXEF!"
exit /b 0