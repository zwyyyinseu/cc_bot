#!/bin/bash
# 停止 cc_bot
cd "$(dirname "$0")"

if [ -f bot.pid ]; then
    PID=$(cat bot.pid)
    kill "$PID" 2>/dev/null && echo "⛔ Bot 已停止 (PID: $PID)"
    rm -f bot.pid
else
    # 兜底：按进程名查找
    pkill -f "main.py" 2>/dev/null && echo "⛔ Bot 已停止"
fi
