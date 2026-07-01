#!/bin/bash
# cc_bot 健康检查 — 一键查看 bot 是否正常运行
cd "$(dirname "$0")/.."   # 回项目根目录

echo "╔══════════════════════════════════════╗"
echo "║       cc_bot 健康检查               ║"
echo "╠══════════════════════════════════════╣"

# 1. 进程存活
if [ -f bot.pid ] && kill -0 $(cat bot.pid) 2>/dev/null; then
    PID=$(cat bot.pid)
    echo "║  进程: 🟢 运行中 (PID: $PID)"
    ALIVE=1
else
    echo "║  进程: 🔴 未运行"
    ALIVE=0
fi

# 2. 运行模式（检查 bot.pid 进程的启动时间 vs 当前时间）
if [ "$ALIVE" = "1" ]; then
    # 用 /status 命令的结果更靠谱：查日志中最后一次状态变更
    LAST_START=$(grep "ACTIVATED by /start" bot.log 2>/dev/null | tail -1)
    LAST_STOP=$(grep "entering idle\|auto-idle" bot.log 2>/dev/null | tail -1)
    LAST_START_TS=$(echo "$LAST_START" | grep -oP '^\d+-\d+ \d+:\d+:\d+' 2>/dev/null || echo "0")
    LAST_STOP_TS=$(echo "$LAST_STOP" | grep -oP '^\d+-\d+ \d+:\d+:\d+' 2>/dev/null || echo "0")
    if [ "$LAST_START_TS" '>' "$LAST_STOP_TS" ] 2>/dev/null || [ "$LAST_STOP_TS" = "0" ]; then
        if [ -n "$LAST_START" ]; then
            echo "║  模式: 🟢 活跃"
        else
            echo "║  模式: 😴 休眠（默认）"
        fi
    else
        echo "║  模式: 😴 休眠中"
    fi

    # 3. 最后活动时间
    LAST_LOG=$(tail -1 bot.log 2>/dev/null)
    echo "║  最近: $(echo "$LAST_LOG" | cut -c1-60)..."
fi

# 4. 看门狗状态
if [ -f .watchdog.lock ] && kill -0 $(cat .watchdog.lock) 2>/dev/null; then
    echo "║  守护: 🟢 看门狗运行中"
else
    echo "║  守护: ⚠️ 无看门狗"
fi

# 5. Token 状态
if [ -f bot.log ]; then
    TOKEN=$(grep "token refreshed" bot.log 2>/dev/null | tail -1 | grep -o "expires in [0-9]*s")
    if [ -n "$TOKEN" ]; then
        echo "║  Token: $TOKEN"
    fi
fi

# 6. Git 版本 + 运行版本
COMMIT=$(git log --oneline -1 2>/dev/null | cut -c1-7)
echo "║  版本: $COMMIT"

# 7. 历史/数据占用
if [ -d data/history ]; then
    HIST_SIZE=$(du -sh data/history 2>/dev/null | cut -f1)
    echo "║  历史: $HIST_SIZE"
fi

echo "╚══════════════════════════════════════╝"
