#!/bin/bash
# 启动 cc_bot（带崩溃自恢复，每天最多重启 3 次）
cd "$(dirname "$0")/.."   # 脚本在 scripts/ 下，回到项目根目录
PROJECT_ROOT="$(pwd)"

# 清理旧进程
pkill -9 -f "cc_bot/.*main.py" 2>/dev/null
pkill -9 -f "cc_bot/.*start.sh" 2>/dev/null
sleep 1

# 确保目录存在
mkdir -p data workspace

# 清空旧日志
> bot.log

# ── 看门狗循环 ──────────────────────────────────────────────────────
MAX_RESTARTS=3
RESTART_COUNT=0
LAST_RESTART_DATE=$(date +%Y%m%d)

while true; do
    # 从 src/ 目录运行 main.py（让同级 imports 正常工作）
    cd "$PROJECT_ROOT/src"
    nohup python3 -u main.py >> "$PROJECT_ROOT/bot.log" 2>&1 &
    PID=$!
    echo $PID > "$PROJECT_ROOT/bot.pid"
    echo "[watchdog] Bot 已启动 (PID: $PID, 今日重启: $RESTART_COUNT/$MAX_RESTARTS)"
    cd "$PROJECT_ROOT"

    # 等待进程退出
    wait $PID 2>/dev/null
    EXIT_CODE=$?

    # 跨天重置计数器
    TODAY=$(date +%Y%m%d)
    if [ "$TODAY" != "$LAST_RESTART_DATE" ]; then
        RESTART_COUNT=0
        LAST_RESTART_DATE=$TODAY
    fi

    # 人为停止（stop.sh 删除了 bot.pid）→ 不重启
    if [ ! -f bot.pid ]; then
        echo "[watchdog] bot.pid 已删除，退出看门狗"
        exit 0
    fi

    RESTART_COUNT=$((RESTART_COUNT + 1))
    if [ $RESTART_COUNT -gt $MAX_RESTARTS ]; then
        echo "[watchdog] 今日重启已达上限 ($MAX_RESTARTS 次)，停止守护"
        exit 1
    fi

    echo "[watchdog] Bot 崩溃 (exit=$EXIT_CODE)，3 秒后重启 ($RESTART_COUNT/$MAX_RESTARTS)..."
    sleep 3
done
