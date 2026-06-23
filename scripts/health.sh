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
else
    echo "║  进程: 🔴 未运行"
fi

# 2. 运行模式
if [ -f bot.log ]; then
    MODE=$(grep -o "poll loop started (idle mode)" bot.log | tail -1)
    if [ -n "$MODE" ]; then
        echo "║  模式: 😴 休眠中"
    else
        echo "║  模式: 🟢 活跃"
    fi

    # 3. 最后活动时间
    LAST_LOG=$(tail -1 bot.log)
    echo "║  最近: $(echo $LAST_LOG | cut -c1-60)..."
fi

# 4. 看门狗状态
if pgrep -f "start.sh" > /dev/null 2>&1; then
    echo "║  守护: 🟢 看门狗运行中"
else
    echo "║  守护: ⚠️ 无看门狗"
fi

# 5. Token 状态
if [ -f bot.log ]; then
    TOKEN=$(grep "token refreshed" bot.log | tail -1 | grep -o "expires in [0-9]*s")
    if [ -n "$TOKEN" ]; then
        echo "║  Token: $TOKEN"
    fi
fi

# 6. Git 版本
COMMIT=$(git log --oneline -1 2>/dev/null | cut -c1-7)
echo "║  版本: $COMMIT"

echo "╚══════════════════════════════════════╝"
