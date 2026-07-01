#!/bin/bash
# cc_bot 启动脚本（带看门狗，每天最多重启 3 次）
set -e
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
LOCKFILE="$PROJECT_ROOT/.watchdog.lock"

# ── 防止重复启动 ──────────────────────────────────────────────────
if [ -f "$LOCKFILE" ]; then
    LOCK_PID=$(cat "$LOCKFILE")
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[watchdog] 已有看门狗运行中 (PID=$LOCK_PID)，退出"
        exit 0
    fi
fi
echo $$ > "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

# ── 杀上一次的 bot（仅按 bot.pid，不误杀其他进程） ──────────────
if [ -f bot.pid ]; then
    OLD_PID=$(cat bot.pid)
    kill "$OLD_PID" 2>/dev/null && echo "[watchdog] 已停止旧 bot (PID=$OLD_PID)" || true
    rm -f bot.pid
fi
sleep 1

# ── 保证目录 ──────────────────────────────────────────────────────
mkdir -p data workspace

# ── 看门狗循环 ────────────────────────────────────────────────────
MAX_RESTARTS=10
RESTART_COUNT=0
LAST_DATE=$(date +%Y%m%d)

while true; do
    cd "$PROJECT_ROOT/src"
    nohup python3 -u main.py >> "$PROJECT_ROOT/bot.log" 2>&1 &
    PID=$!
    echo $PID > "$PROJECT_ROOT/bot.pid"
    echo "[watchdog] Bot 已启动 (PID=$PID, 重启次数: $RESTART_COUNT/$MAX_RESTARTS)"
    cd "$PROJECT_ROOT"

    wait $PID 2>/dev/null
    EXIT_CODE=$?

    # 人为停止 → 退出
    if [ ! -f bot.pid ]; then
        echo "[watchdog] 手动停止，看门狗退出"
        exit 0
    fi

    # 跨天重置
    TODAY=$(date +%Y%m%d)
    [ "$TODAY" != "$LAST_DATE" ] && RESTART_COUNT=0 && LAST_DATE=$TODAY

    RESTART_COUNT=$((RESTART_COUNT + 1))
    if [ $RESTART_COUNT -gt $MAX_RESTARTS ]; then
        echo "[watchdog] 今日重启次数已达上限 ($MAX_RESTARTS)，退出"
        exit 1
    fi

    echo "[watchdog] Bot 异常退出 (code=$EXIT_CODE)，3 秒后重启..."
    sleep 3
done
