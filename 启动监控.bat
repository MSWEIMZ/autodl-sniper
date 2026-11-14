@echo off
cd /d %~dp0
title AutoDL 抢卡监控
echo 正在启动监控服务...
echo 浏览器会自动打开 http://127.0.0.1:8917
echo 关闭本窗口即停止监控
python autodl_monitor.py
pause
