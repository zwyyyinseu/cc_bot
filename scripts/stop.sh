#!/bin/bash
# 停止 cc_bot
cd "$(dirname "$0")/.."

if [ -f bot.pid ]; then
    PID=$(cat bot.pid)
    kill "$PID" 2>/dev/null && echo "⛔ Bot 已停止 (PID=$PID)"
    rm -f bot.pid
    # 看门狗检测到 bot.pid 被删会自动退出
    echo "⛔ 看门狗将自动退出"
else
    echo "ℹ️ bot.pid 不存在，bot 可能未运行"
fi
