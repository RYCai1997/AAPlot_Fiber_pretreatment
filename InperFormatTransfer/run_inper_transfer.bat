@echo off
cd /d "%~dp0"
python inper_transfer_gui.py
if errorlevel 1 pause
