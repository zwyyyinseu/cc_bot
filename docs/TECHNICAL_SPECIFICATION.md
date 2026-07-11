# cc_bot 技术规格说明书

> 版本 1.3 | 2026-07-11 | Apache 2.0

---

## 1. 项目概述

### 1.1 产品定义

cc_bot 是一个基于飞书消息的 Claude Code 移动端操控工具。用户在飞书手机端发送消息，bot 在服务器上拉起 Claude CLI 子进程执行任务，结果通过飞书 interactive 卡片（Schema 2.0）流式回传。

### 1.2 核心指标

| 指标 | 值 |
|------|-----|
| 代码规模 | ~2,500 行 Python + ~140 行 Shell |
| 测试用例 | 35 个，覆盖 8 个维度 |
| 外部依赖 | 仅 httpx（零框架依赖） |
| 运行时内存 | < 100 MB |
| 消息延迟 | 轮询间隔 2s（活跃）/ 10s（休眠） |
| 崩溃恢复 | 看门狗自动重启，每日上限 10 次 |

### 1.3 目标用户

个人开发者，单实例部署，通过飞书移动端操控服务器上的 Claude Code。

---

## 2. 系统架构

### 2.1 整体拓扑

```
┌──────────────┐                    ┌─────────────────────────────────┐
│  飞书客户端    │                    │           服务器                  │
│  (手机/PC)    │                    │                                 │
│              │   HTTPS/REST API   │  ┌──────────┐   stream-json    │
│  用户发消息   │ ◄─────────────────► │  │  cc_bot   │ ◄──────────────► │
│              │  卡片流式推送       │  │  (asyncio) │   stdin/stdout   │
│              │                    │  └──────────┘                  │
└──────────────┘                    │       │  Claude CLI 子进程       │
                                    │       │  (独立进程，双协程管理)   │
                                    └───────┼──────────────────────────┘
                                            │
                                    ┌───────┴──────────────────────────┐
                                    │  ~/.claude/projects/              │
                                    │  └── <sanitized-path>/            │
                                    │       └── <session-id>.jsonl      │
                                    └──────────────────────────────────┘
```

### 2.2 进程架构

```
┌─ start.sh (看门狗) ─────────────────────────────────────┐
│  lockfile: .watchdog.lock                               │
│  pidfile:  bot.pid                                      │
│                                                         │
│  while true:                                            │
│    python3 -u main.py  →  bot.log                       │
│    wait $PID                                            │
│    if bot.pid deleted: exit 0    # 手动停止              │
│    restart_count++                                       │
│    if restart_count > 10: exit 1 # 异常退出保护          │
│    sleep 3; continue                                     │
└─────────────────────────────────────────────────────────┘
         │
         ▼
┌─ main.py (Bot 主进程) ──────────────────────────────────┐
│  asyncio event loop                                      │
│                                                         │
│  ┌─ _poll_loop() ──────────────────────────────────┐    │
│  │  while True:                                     │    │
│  │    messages = feishu.get_messages(chat_id)       │    │
│  │    for msg in new_messages:                      │    │
│  │      await _handle_message(msg)                  │    │
│  │    sleep(interval)                               │    │
│  └──────────────────────────────────────────────────┘    │
│                                                         │
│  ┌─ _handle_message() ─────────────────────────────┐    │
│  │  去重 → 命令路由 → 飞书文档检测 → 启动 Claude       │    │
│  └──────────────────────────────────────────────────┘    │
│                                                         │
│  ┌─ run_claude_and_stream() ───────────────────────┐    │
│  │  stream-json stdout 解析 → 卡片流式推送            │    │
│  └──────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

### 2.3 模块依赖图

```
main.py
  ├── config.py          # 配置管理（.env + 环境变量）
  ├── state.py           # 全局状态持久化（state.json）
  ├── feishu_client.py   # 飞书 REST API 客户端
  ├── conversations.py   # 多对话管理（conversations.json）
  ├── stream_handler.py  # Claude 输出 → 飞书卡片流式
  │     ├── claude_runner.py  # Claude CLI 子进程管理
  │     └── feishu_client.py  # 卡片更新 / 文件上传
  └── history_store.py   # 对话历史持久化（JSONL）
```

依赖方向：`config.py` ← 所有模块 | `feishu_client.py` ← `main.py` / `stream_handler.py`

---

## 3. 模块详设

### 3.1 config.py — 配置管理

**职责**：从 `.env` 文件和环境变量读取配置，零外部依赖的手动解析器。

**设计决策**：
- 不使用 python-dotenv，减少依赖
- 环境变量优先级高于 `.env` 文件，兼容 Docker/K8s 部署
- 所有可调参数集中到 `Config` dataclass，消除硬编码

**关键配置项**：

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `POLL_INTERVAL` | 2.0s | 活跃态消息轮询间隔 |
| `IDLE_POLL_INTERVAL` | 10.0s | 休眠态消息轮询间隔 |
| `AUTO_IDLE_SEC` | 1800.0s | 无消息自动休眠阈值 |
| `RESULT_TIMEOUT` | 600.0s | 单轮 Claude 执行超时 |
| `THROTTLE_INTERVAL` | 5.0s | 卡片更新节流间隔 |
| `CARD_MAX_CHARS` | 4000 | 卡片内容上限 |
| `HISTORY_FILE_MAX_LINES` | 500 | 历史文件行数上限 |
| `HISTORY_FILE_MAX_BYTES` | 2MB | 历史文件大小上限 |

### 3.2 feishu_client.py — 飞书 API 客户端

**职责**：封装飞书 REST API，自动管理 token，提供消息收发、卡片更新、文件上传、文档读取能力。

**核心设计**：

```
Token 管理（双检锁模式）：
  _ensure_token()
    ├── 快速路径：token 有效 → 直接返回
    └── 慢路径：_token_lock.acquire()
         ├── 双重检查（可能已被其他协程刷新）
         └── _refresh_token()

请求重试（透明恢复）：
  _request()
    ├── 401 → 刷新 token + 重试
    └── code=99991663 → 刷新 token + 重试
```

**API 方法矩阵**：

| 方法 | HTTP | 端点 | 用途 |
|------|------|------|------|
| `get_messages` | GET | `/im/v1/messages` | 轮询消息列表 |
| `reply_message` | POST | `/im/v1/messages/{id}/reply` | 回复卡片/文件 |
| `update_card` | PATCH | `/im/v1/messages/{id}` | 流式更新卡片 |
| `send_message` | POST | `/im/v1/messages` | P2P 发送 |
| `upload_file` | POST | `/im/v1/files` | 上传文件 |
| `fetch_document` | GET | `/docx/v1/documents/{token}/raw_content` | 读取飞书文档 |

**文件上传兼容性处理**：

- `file_type` 仅使用飞书认可的有限类型（pdf/doc/xls/ppt/stream）
- 中文文件名 sanitize 为 ASCII 安全名称
- httpx multipart 使用纯 dict 格式（非 tuple）

### 3.3 claude_runner.py — Claude CLI 子进程管理

**职责**：管理 Claude CLI 子进程生命周期，实现 stream-json 协议的读写。

**双协程 stdin 管理**：

```
          ┌──────────────────┐
          │  message_queue    │
          │  (asyncio.Queue)  │
          └────────┬─────────┘
                   │
     ┌─────────────┴─────────────┐
     ▼                           ▼
┌─────────────┐          ┌──────────────────┐
│ _stdin_writer│          │_stdin_close_checker│
│              │          │                  │
│ 消息到达即写入 │          │ result 后检查队列  │
│ stdin（不等    │          │ 空 → sentinel     │
│ result）      │          │ 非空 → 继续等待    │
│              │          │                  │
│ 超时保护:     │          │ RESULT_TIMEOUT    │
│ 无            │          │ 600s → sentinel   │
└──────┬───────┘          └────────┬─────────┘
       │                           │
       └───────────┬───────────────┘
                   ▼
           proc.stdin.close()
```

**stream-json 协议流**：

```
stdin (JSONL):
  {"type":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]}}

stdout (JSONL):
  {"type":"system","subtype":"init","sessionId":"..."}
  {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
  {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",...}]}}
  {"type":"user","message":{"content":[{"type":"tool_result",...}]}}
  {"type":"result","cost_usd":0.123}
```

**子进程启动重试**：最多 3 次，间隔 0.5s，兼容 Python 3.8 的 `create_subprocess_exec` 竞态条件。

### 3.4 stream_handler.py — 流式输出处理

**职责**：解析 Claude stream-json 输出，转换为飞书卡片流式推送。

**事件处理状态机**：

```
                    ┌─────────┐
         session_init│         │ new_round
              ┌─────►  空闲    ◄─────┐
              │     │         │     │
              │     └────┬────┘     │
              │          │ text     │
              │          ▼          │
              │     ┌─────────┐     │
              │     │ 文本累积 │     │
              │     │ buf+=   │     │
              │     └────┬────┘     │
              │   tool_call│        │
              │          ▼          │
              │     ┌─────────┐     │
              │     │ 工具执行 │     │
              │     │ tool_log│     │
              │     └────┬────┘     │
              │  tool_result│       │
              │          ▼          │
              │     ┌─────────┐     │
              │     │ 结果分析 │     │
              │     └────┬────┘     │
              │     result│         │
              │          ▼          │
              │     ┌─────────┐     │
              └────►│ 完成推送 │     │
                    └─────────┘     │
                         └──────────┘
```

**节流策略**：

- 中间文本：最少间隔 `THROTTLE_INTERVAL`（5s）更新卡片
- 工具切换 / result 事件：`force=True` 立即推送
- update_card 失败 → 自动 fallback 到 reply_message（新卡片）

**思考计时器**（`_thinking_ticker`）：

| 阶段 | 条件 | 行为 |
|------|------|------|
| 思考中 | 无任何输出，< 60s | 动画图标 + 计时 |
| 复杂任务 | 无输出，60-120s | 提示"复杂任务处理中" |
| 疑似卡死 | 无输出，> 120s | 警告"可能网络异常" |
| 工具执行中 | tool_log 非空，≥ 15s | 追加执行耗时 |

**推送通知**：result 事件后 `reply_message` 发送新消息触发飞书手机推送，用户无需一直盯着屏幕。

### 3.5 conversations.py — 多对话管理

**数据模型**：

```python
@dataclass
class Conversation:
    id: str           # 8 位短 UUID
    title: str        # 用户自定义标题
    session_id: str   # Claude --resume 会话 ID
    workspace: str    # 工作目录路径
    created_at: str   # ISO 时间戳
    updated_at: str   # 最后一次活跃时间
```

**持久化**：`data/conversations.json`，原子写入（写 `.tmp` → `rename`）。

**会话状态指示**：通过检查 `~/.claude/projects/` 下 session JSONL 文件是否存在，显示 💾（有上下文）/ 🆕（新对话）/ ⚠️（丢失）。

### 3.6 history_store.py — 历史持久化

**格式**：每对话一个 JSONL 文件（`data/history/{conv_id}.jsonl`）。

```json
{"role":"user","text":"帮我分析这段代码","timestamp":"2026-07-11T01:23:45+00:00"}
{"role":"assistant","text":"这段代码实现了...","timestamp":"2026-07-11T01:24:12+00:00"}
```

**截断策略**（双重保护）：

| 条件 | 触发值 |
|------|--------|
| 行数 > `HISTORY_FILE_MAX_LINES × 2` | 1,000 行 |
| 文件大小 > `HISTORY_FILE_MAX_BYTES × 2` | 4 MB |

截断时保留最近 `HISTORY_FILE_MAX_LINES` 条（500 行）。

**Claude Session 导入**：`/switch` 和 `/history` 时自动从 Claude 原生 session JSONL 导入对话历史。兼容 content 为字符串或列表的两种格式，自动跳过子代理消息。

### 3.7 main.py — 消息路由与状态机

**命令路由表**：

```
EXACT 匹配（无参数）：
  /list, /ls    → _cmd_list
  /stop         → _cmd_stop
  /start        → _cmd_start
  /status, /stat→ _cmd_status
  /help, /h, /? → _cmd_help

PREFIX 匹配（带参数）：
  /new <title>  → _cmd_new
  /rename <name>→ _cmd_rename
  /switch <idx> → _cmd_switch
  /del <idx>    → _cmd_del
  /view <path>  → _cmd_view
  /history [N]  → _cmd_history
```

**休眠/唤醒状态机**：

```
     ┌──────────┐  /start   ┌──────────┐
     │   休眠    │ ────────► │   活跃    │
     │ 10s 轮询  │          │ 2s 轮询   │
     │ 仅响应     │ ◄──────── │ 全功能     │
     │ /start     │ /stop    │           │
     │ /status    │ 无消息    │           │
     │ /help      │ 30min    │           │
     └──────────┘          └──────────┘
```

**去重机制**：基于 `last_message_ts`（秒级时间戳）+ `last_message_id`（同秒内区分）。在 `_handle_message` 第一时间设置，避免 asyncio 协程切换导致重复处理。

**飞书文档检测**：正则匹配 URL → `fetch_document()` 拉取内容 → 拼接为 Claude prompt 上下文。拉取失败时显示权限提示，不发空请求。

---

## 4. 数据流

### 4.1 完整请求链路

```
User (飞书)
  │
  │  "帮我分析这段 Verilog 代码"
  │
  ▼
Feishu API ──GET /im/v1/messages──► cc_bot (poll_loop)
  │                                      │
  │  消息 JSON                            │ 去重 (last_message_ts + message_id)
  │                                      │ 命令路由 (EXACT/PREFIX dict)
  │                                      │ 飞书文档检测 (regex → fetch_document)
  │                                      ▼
  │                              claude_runner.run_claude()
  │                                      │
  │                                      │ stream-json stdin:
  │                                      │ {"type":"user","message":{...}}
  │                                      ▼
  │                              Claude CLI 子进程
  │                                      │
  │                                      │ stream-json stdout:
  │                                      │ {"type":"assistant",...}
  │                                      │ {"type":"result",...}
  │                                      ▼
  │                              stream_handler.on_event()
  │                                      │
  │                                      │ text → buf 累积
  │                                      │ tool_call → tool_log 记录
  │                                      │ result → 最终推送
  │                                      ▼
  │                              feishu_client.update_card() / reply_message()
  │                                      │
  │  ◄── PATCH /im/v1/messages/{id} ────┘
  │  卡片流式更新 (Markdown)
  │
  ▼
User (飞书) 看到实时回复
```

### 4.2 多轮对话数据流

```
第 1 轮                      第 2 轮                      第 N 轮
User ──► queue.put(msg1)    User ──► queue.put(msg2)    ...
              │                        │
              ▼                        ▼
         _stdin_writer           _stdin_writer
              │                        │
              ▼                        ▼
         proc.stdin               proc.stdin
              │                        │
              ▼                        ▼
         Claude 处理              Claude 处理
              │                        │
              ▼                        ▼
         result_event.set()       result_event.set()
              │                        │
              ▼                        ▼
    _stdin_close_checker        _stdin_close_checker
    queue.empty()? → No         queue.empty()? → Yes → sentinel → close stdin
```

---

## 5. 关键设计决策

### 5.1 为何使用 REST 轮询而非 WebSocket

| 维度 | REST 轮询 | WebSocket 长连接 |
|------|----------|-----------------|
| 实现复杂度 | 低（HTTP GET + sleep） | 高（心跳、重连、事件路由） |
| 可靠性 | 请求失败重试即可 | 断连需完整重连逻辑 |
| 延迟 | 2s（可接受） | 实时（< 100ms） |
| 资源占用 | 极低 | 需维持 TCP 连接 |

**决策**：REST 轮询。对于移动端文本对话场景，2 秒延迟完全可接受；实现的简洁性和可靠性远优于 WebSocket。

### 5.2 为何保持 stdin 打开（多轮模式）

传统做法：每轮对话启动新 Claude 进程 → 冷启动 3-5s + 丢失上下文。

多轮模式：
- Claude 进程常驻，stdin 保持打开
- 新消息通过 queue 异步写入，不等 result
- 上下文自然累积在 Claude 进程中
- 切换轮次零延迟（管道已在内存中）

代价：需要双协程管理（writer + close_checker），复杂度高于单轮模式，但可靠性已验证稳定。

### 5.3 为何使用 subprocess 而非 Claude SDK

- Claude CLI 的 `--resume` 提供原生会话持久化
- stream-json 协议稳定且文档完备
- 避免 SDK 版本锁定的维护负担
- bot 可接入任意兼容 CLI（包括 DeepSeek 等第三方模型）

### 5.4 卡片更新 vs 新消息

- **流式中间过程**：PATCH 更新同一张卡片（`update_card`），减少消息轰炸
- **最终结果**：POST 新消息（`reply_message`），触发手机推送通知
- **update_card 失败**：自动 fallback 为新消息，保证不丢信息

### 5.5 原子写入策略

所有持久化文件（`state.json`、`conversations.json`）采用"写 `.tmp` → `rename`"策略，避免写入中途崩溃导致文件损坏。

---

## 6. 开发迭代记录

| 阶段 | 版本 | 关键交付 | 发现问题 |
|------|------|---------|---------|
| MVP | v1.0 | 多轮对话、流式推送、对话管理 | — |
| 质量建设 | v1.1 | 休眠/唤醒、命令路由重构、logging、看门狗 | 看门狗自杀、kill 误杀 |
| 功能扩展 | v1.2 | 文档读取、文件上传、目录浏览、历史回溯 | 文件上传 234001、wiki 内容为空 |
| 体验优化 | v1.3 | 推送通知、双协程重构、超时保护、输出截断 | 并发去重失效、卡片空白 |
| 稳定性加固 | v1.4 | 自动休眠保护 Claude 进程、历史文件大小截断、健康检查增强 | auto-idle 误杀运行中的任务 |
| 开源准备 | v1.5 | 隐私审计、git 历史清理、Apache 2.0 许可、中英文 README | 硬编码凭证泄露 |

---

## 7. 测试策略

### 7.1 测试范围

| 测试维度 | 用例数 | 覆盖内容 |
|---------|--------|---------|
| 并发队列 | 2 | 消息入队、sentinel 终止 |
| 超长/空文本 | 3 | 截断、空输入、markdown 特殊字符 |
| 状态文件损坏 | 3 | 损坏/缺失 state.json、conversations.json |
| 对话 CRUD | 7 | 创建、切换、删除、边界（空、重名、超长标题） |
| 历史文件 | 4 | 空对话、快速追加、损坏行、换行符 |
| stream-json | 3 | 正常/特殊字符/Unicode 消息构建 |
| 命令注册 | 1 | 所有 `_cmd_` 方法存在性 |
| 配置完整性 | 2 | 字段存在性、默认值合理性 |
| 历史存储 | 7 | 追加、跳过空消息、读取、删除、截断、导入、配置值 |
| 真实环境 | 1 | Claude session JSONL 导入 |
| **合计** | **35** | |

### 7.2 运行方式

```bash
python3 -m pytest tests/ -v          # 全量
python3 -m pytest tests/ -v -k "test_config"  # 按名称筛选
```

### 7.3 测试隔离

使用 `pytest.fixture(autouse=True)` 将 `DATA_DIR` 重定向到临时目录，测试结束后恢复原始状态。避免测试污染运行时数据（历史上曾导致 `conversations.json` 被覆盖）。

---

## 8. 运维手册

### 8.1 启动

```bash
bash scripts/start.sh         # 后台启动（含看门狗）
bash scripts/health.sh        # 验证运行状态
```

### 8.2 停止

```bash
bash scripts/stop.sh          # 停止 bot + 看门狗自动退出
```

### 8.3 日志

```bash
tail -f bot.log               # 实时日志
grep "ERROR\|timeout\|network" bot.log  # 排查异常
```

### 8.4 健康检查输出示例

```
╔══════════════════════════════════════╗
║       cc_bot 健康检查               ║
╠══════════════════════════════════════╣
║  进程: 🟢 运行中 (PID: 4158518)
║  模式: 🟢 活跃
║  最近: 07-01 17:11:59 token refreshed...
║  守护: 🟢 看门狗运行中
║  Token: expires in 6314s
║  版本: 03f5785
║  历史: 296K
╚══════════════════════════════════════╝
```

### 8.5 异常恢复机制

```
进程崩溃   →  看门狗 wait() 返回 → 自动重启（每日 ≤ 10 次）
网络中断   →  指数退避 2^n × 轮询间隔 → 自动恢复
API 超时   →  RESULT_TIMEOUT (600s) → 关闭 stdin → 用户看到超时卡片
token 过期 →  401/1663 自动刷新 + 重试
状态损坏   →  加载失败返回默认值，不崩溃
```

---

## 9. 安全设计

### 9.1 访问控制

- 仅响应 `FEISHU_OPEN_ID` 配置的用户消息（`sender_open_id` 校验）
- 休眠模式下仅响应 `/start`、`/status`、`/help`

### 9.2 凭证管理

- `.env` 文件不入 Git（`.gitignore`）
- `.env.example` 提供模板，不含真实凭证
- 飞书 token 内存缓存，过期前 5 分钟自动刷新
- 双检锁模式防止并发 token 刷新

### 9.3 运行时安全

- Claude 进程运行在 `IS_SANDBOX=1` 环境变量下
- `--dangerously-skip-permissions` 仅用于无头模式（服务器环境下用户无法交互确认）
- 子进程环境清理 `CLAUDECODE` 变量，防止嵌套检测

### 9.4 数据持久化

- 所有数据存储于 `data/` 目录（加入 `.gitignore`）
- 历史文件仅保留最近 500 条 / 2MB
- 删除对话时同步清理历史文件

---

## 10. 附录

### A. 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| 语言 | Python | 3.11+ |
| 异步框架 | asyncio | 标准库 |
| HTTP 客户端 | httpx | ≥ 0.24.0 |
| Claude 接口 | Claude CLI stream-json | 2.1.x |
| 消息平台 | 飞书 REST API | Schema 2.0 |
| 测试 | pytest | ≥ 7.0 |

### B. 目录结构

```
cc_bot/
├── src/
│   ├── main.py              # 入口 + 消息路由 + 状态机
│   ├── claude_runner.py     # Claude CLI 子进程管理
│   ├── stream_handler.py    # 流式输出 → 卡片推送
│   ├── feishu_client.py     # 飞书 API 客户端
│   ├── conversations.py     # 多对话管理
│   ├── history_store.py     # 对话历史持久化
│   ├── state.py             # 全局状态持久化
│   └── config.py            # 配置管理
├── tests/
│   ├── test_boundary.py     # 核心引擎边界测试 (27 用例)
│   └── test_history_store.py # 历史存储测试 (8 用例)
├── scripts/
│   ├── start.sh             # 启动脚本（看门狗）
│   ├── stop.sh              # 停止脚本
│   └── health.sh            # 健康检查
├── docs/
│   ├── TECHNICAL_SPECIFICATION.md  # 本文件
│   ├── 使用指南.md                  # 用户手册
│   └── 飞书机器人从0到1.md          # 搭建教程
├── requirements.txt         # Python 依赖
├── .env.example             # 配置模板
├── README.md                # 中文说明
├── README_EN.md             # 英文说明
└── LICENSE                  # Apache 2.0
```
