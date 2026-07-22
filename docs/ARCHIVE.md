# cc_bot 项目归档说明

> 归档日期：2026-07-22 | 版本：v1.5 | 状态：功能完整，稳定运行中

---

## 1. 项目概述

**cc_bot** 是一个基于飞书消息的 Claude Code 移动端操控工具。用户在飞书手机端发送消息，bot 在服务器上拉起 Claude CLI 子进程执行任务，结果通过飞书 interactive 卡片流式回传。

**GitHub**：[zwyyyinseu/cc_bot](https://github.com/zwyyyinseu/cc_bot) | **License**：Apache 2.0

---

## 2. 完成度评估

### 2.1 核心功能（✅ 全部完成）

| 功能 | 状态 | 说明 |
|------|------|------|
| 多轮对话 | ✅ | Claude 进程常驻，stdin 保持打开 |
| 流式推送 | ✅ | stream-json → 飞书卡片实时更新 |
| 多对话管理 | ✅ | 创建/切换/删除/重命名，最多支持不限量对话 |
| 命令系统 | ✅ | 11 个命令，精确匹配 + 前缀匹配路由表 |
| 对话历史 | ✅ | 每对话独立 JSONL，自动从 Claude session 导入 |
| 休眠/唤醒 | ✅ | 30 分钟无消息自动休眠，Claude 运行中不会误休眠 |
| 文件浏览 | ✅ | `/view` 目录树 + 文件上传，路径穿越防护 |
| 飞书文档读取 | ✅ | 支持 docx/wiki 链接，自动拼接为 Claude 上下文 |
| 推送通知 | ✅ | result 后新消息触发手机推送 |
| 长输出处理 | ✅ | 超 4000 字符自动保存文件 + 发送到飞书 |
| 花费统计 | ✅ | `/cost` 今日/本月/总计，自动记录 + 增量导入 |
| 安全防护 | ✅ | 仅响应配置的 Open ID，路径穿越防护，凭证不入 Git |

### 2.2 运维能力（✅ 全部完成）

| 能力 | 状态 | 说明 |
|------|------|------|
| 看门狗 | ✅ | 崩溃自动重启，每日 ≤ 10 次，锁文件防重复 |
| 健康检查 | ✅ | `health.sh`：进程/模式/Token/版本/历史占用 |
| 配置管理 | ✅ | `.env` + 环境变量，零额外依赖解析 |
| 日志 | ✅ | 带时间戳的轮转日志（bot.log） |
| 异常恢复 | ✅ | 网络中断指数退避、token 过期自动刷新、超时保护 |
| 测试 | ✅ | 35 个用例，8 个维度，pytest 执行 |
| 文档 | ✅ | 中英文 README、技术规格说明书、搭建教程、用户指南 |

### 2.3 已知限制

| 限制 | 影响 | 根因 |
|------|------|------|
| 不支持图片 | 无法分析截图/框图 | DeepSeek API 暂不支持多模态 |
| 服务器单点 | 服务器宕机 = bot 不可用 | 个人工具不作多机热备 |
| 历史花费不可回溯 | 之前的 API 费用查不到 | Claude session 文件不持久化 result 事件 |
| 轮询延迟 ~2s | 消息响应非实时 | REST 轮询而非 WebSocket，是设计选择 |

---

## 3. 开发历程

### 3.1 迭代时间线

| 阶段 | 时间 | 关键交付 | 解决的核心问题 |
|------|------|---------|-------------|
| MVP | 2026-06 | 多轮对话、流式推送、对话管理 | "能不能在手机上操控 Claude" |
| 质量建设 | 06 中旬 | 休眠唤醒、命令路由重构、logging、看门狗 | "bot 老死、重复回复" |
| 功能扩展 | 06 下旬 | 文档读取、文件上传、目录浏览、历史回溯 | "想读飞书文档、想看文件" |
| 体验优化 | 07 上旬 | 推送通知、双协程重构、超时保护、输出截断 | "等回复不知道什么时候好" |
| 稳定性加固 | 07 中旬 | auto-idle 保护、历史文件大小截断、健康检查增强 | "怕误杀正在跑的 Claude" |
| 开源准备 | 07 下旬 | 隐私审计、git 历史清理、Apache 2.0、中英文文档 | "想开源但担心隐私泄露" |

### 3.2 关键决策记录

1. **REST 轮询而非 WebSocket**：可靠性 > 实时性，2 秒延迟可接受
2. **多轮模式（stdin 常开）而非每轮启动新进程**：消除冷启动，保留上下文
3. **subprocess 调用 CLI 而非 SDK**：避免版本锁定，兼容第三方模型
4. **零框架依赖（仅 httpx）**：最小化维护负担
5. **双协程 stdin 管理**：writer 立即写入 + close_checker 超时保护

---

## 4. 技术栈

| 组件 | 技术 | 备注 |
|------|------|------|
| 语言 | Python 3.11+ | 标准库 asyncio |
| HTTP | httpx ≥ 0.24.0 | 唯一外部依赖 |
| Claude 接口 | Claude CLI stream-json | v2.1.x |
| 消息平台 | 飞书 REST API | Schema 2.0 卡片 |
| 测试 | pytest | 35 用例 |
| 部署 | Shell 脚本 + 看门狗 | systemd 可选但不必要 |

---

## 5. 项目结构

```
cc_bot/
├── src/                          # 源码（~2,500 行）
│   ├── main.py                   # 入口：消息路由 + 状态机
│   ├── claude_runner.py          # Claude CLI 子进程管理（双协程）
│   ├── stream_handler.py         # stream-json → 飞书卡片流式
│   ├── feishu_client.py          # 飞书 REST API 客户端
│   ├── conversations.py          # 多对话管理
│   ├── history_store.py          # 对话历史持久化
│   ├── cost_tracker.py           # API 花费统计
│   ├── state.py                  # 全局状态持久化
│   └── config.py                 # 配置管理
├── tests/                        # 测试（35 用例）
│   ├── test_boundary.py          # 核心引擎边界测试（27）
│   └── test_history_store.py     # 历史存储测试（8）
├── scripts/                      # 运维脚本
│   ├── start.sh                  # 启动（看门狗）
│   ├── stop.sh                   # 停止
│   └── health.sh                 # 健康检查
├── docs/                         # 文档
│   ├── ARCHIVE.md                # 本文件（项目归档说明）
│   ├── TECHNICAL_SPECIFICATION.md # 技术规格说明书
│   ├── 使用指南.md                # 用户手册
│   └── 飞书机器人从0到1.md        # 搭建教程
├── requirements.txt              # Python 依赖
├── .env.example                  # 配置模板
├── README.md / README_EN.md      # 中英文说明
└── LICENSE                       # Apache 2.0
```

---

## 6. 维护指南

### 6.1 日常操作

```bash
bash scripts/start.sh          # 启动
bash scripts/stop.sh           # 停止
bash scripts/health.sh         # 健康检查
tail -f bot.log                # 实时日志
python3 -m pytest tests/ -v    # 运行测试
```

### 6.2 日志关键词

| 关键词 | 含义 |
|--------|------|
| `timeout` / `TIMEOUT` | Claude 执行超时（> 10 分钟） |
| `get_messages failed` | 飞书 API 临时故障（code=2200） |
| `poll loop error` | 网络层异常（httpx 连接失败） |
| `auto-idle` | 正常休眠 |
| `token refreshed` | Token 正常刷新（每 2 小时） |
| `watchdog` | 看门狗活动 |

### 6.3 数据文件

| 文件 | 用途 | 大小上限 |
|------|------|---------|
| `data/conversations.json` | 对话元数据 | ~KB |
| `data/state.json` | 全局状态 | ~200B |
| `data/history/*.jsonl` | 对话历史 | 500 行 / 2MB per file |
| `data/costs.jsonl` | 花费记录 | 无限（每条约 100B） |

---

## 7. 未来扩展方向

以下是不适合当前阶段但将来可能有趣的方向：

- **多模态支持**：取决于 DeepSeek 对 Anthropic 协议的图片能力支持
- **对话导出**：`/export` → 渲染为 Markdown 文件
- **消息模板**：高频问题一键发送
- **Web Dashboard**：浏览器端查看日志、花费、对话列表
- **通知增强**：连续 poll error 阈值告警
- **多用户支持**：当前仅响应单个 Open ID

以上均属于"如果将来需要"类需求，当前版本无缺失。

---

## 8. 总结

cc_bot v1.5 是一个**功能完整、运行稳定、文档齐全**的个人工具项目。经过 6 个阶段的持续迭代、91 次提交、35 个测试用例的覆盖，项目已达到设计目标：**让用户在手机飞书上像操作电脑终端一样使用 Claude Code**。

没有过度设计，没有未完成的功能承诺，没有技术债务。可以归档。
