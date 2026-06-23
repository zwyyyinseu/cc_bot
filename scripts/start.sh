#!/bin/bash
# 启动 cc_bot（带崩溃自恢复，每天最多重启 3 次）
cd "$(dirname "$0")/.."   # 脚本在 scripts/ 下，回到项目根目录
PROJECT_ROOT="$(pwd)"

# 清理旧进程
# 1) 按 bot.pid 杀上一次的 bot 进程
if [ -f bot.pid ]; then
    OLD_PID=$(cat bot.pid)
    kill "$OLD_PID" 2>/dev/null && echo "[watchdog] killed old bot PID=$OLD_PID"
fi
# 2) 杀其他可能残留的 main.py（排除即将启动的新进程）
for pid in $(pgrep -f "main.py" 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
done
# 3) 杀旧的 start.sh 看门狗（排除当前脚本自己）
MYPID=$$
for pid in $(pgrep -f "start.sh" 2>/dev/null); do
    [ "$pid" != "$MYPID" ] && kill -9 "$pid" 2>/dev/null
done
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
