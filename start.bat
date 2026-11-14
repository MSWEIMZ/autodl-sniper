@echo off
cd /d %~dp0
title AutoDL GPU Sniper
echo Starting monitor... http://127.0.0.1:8917
echo Close this window to stop.
python autodl_monitor.py
pause
