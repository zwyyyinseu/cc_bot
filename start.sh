#!/bin/bash
# 启动 cc_bot
cd "$(dirname "$0")"

# 清理旧进程（防止多进程累积导致重复回复）
pkill -9 -f "main.py" 2>/dev/null
sleep 1

# 确保目录存在
mkdir -p data workspace

# 清空旧日志，-u 禁用输出缓冲
> bot.log
nohup python3 -u main.py >> bot.log 2>&1 &
echo $! > bot.pid
echo "🟢 Bot 已启动 (PID: $!)"
echo "查看日志: tail -f bot.log"
