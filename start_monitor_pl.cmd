@echo off
cd /d "%~dp0"
python monitor_pl.py
if errorlevel 1 pause
