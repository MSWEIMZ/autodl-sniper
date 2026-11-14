#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "AutoDL 监控面板: http://127.0.0.1:8917"
exec python3 autodl_monitor.py
