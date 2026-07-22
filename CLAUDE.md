# CLAUDE.md — cc_bot 项目交接文档

> 写给下一个 AI 开发者：读完本文档即可完全理解项目架构、设计意图和代码细节，无需翻阅对话历史。

---

## 你是谁

你是一个 AI 开发助手，要接着开发 cc_bot 项目。这个项目已经完成了 6 个阶段的迭代，当前处于**功能完整、稳定运行**状态。你的任务是理解现有代码，在此基础上做增强或修复。

## 项目是什么

**cc_bot** = 飞书消息 → 服务器 Claude CLI 执行 → 流式卡片回复。

用户是电子专业研究生，做 CPU/FPGA 设计。通过飞书手机端发消息操控服务器上的 Claude Code（实测接入的是 DeepSeek API），结果实时流式回传。不是产品，不做多人支持，不处理复杂权限。

核心价值：不在工位也能写代码、问技术问题。

## 技术架构一句话

`asyncio` 单事件循环，REST 轮询飞书消息，subprocess 拉起 Claude CLI，stream-json 协议通信，双协程管理 stdin。

## 项目结构速查

```
src/
├── main.py              # 入口：事件循环、消息轮询、命令路由、状态机
├── claude_runner.py     # Claude CLI 子进程生命周期（最复杂的模块）
├── stream_handler.py    # Claude stream-json 输出 → 飞书卡片流式
├── feishu_client.py     # 飞书 REST API 封装（token/消息/文件/文档）
├── conversations.py     # 多对话管理（ConversationStore 单例）
├── history_store.py     # 对话历史 JSONL 持久化
├── cost_tracker.py      # API 花费统计（最新添加的模块）
├── state.py             # 全局状态（chat_id, last_message_ts/id）
├── config.py            # .env + 环境变量 → Config dataclass
scripts/
├── start.sh             # 看门狗启动（lockfile + pidfile + 自动重启）
├── stop.sh              # 停止（删 bot.pid → 看门狗自动退出）
├── health.sh            # 健康检查
tests/
├── test_boundary.py     # 27 个边界测试
├── test_history_store.py # 8 个历史存储测试
```

## 每个文件干什么、为什么这样设计

### config.py
- 手动解析 .env（不用 python-dotenv，零依赖原则）
- 环境变量优先级 > .env（兼容 Docker/K8s）
- 所有可调参数集中到 Config dataclass
- `setup_logging()` 输出到 stderr，start.sh 重定向到 bot.log

### state.py
- 持久化 `data/state.json`（chat_id、去重标记、活跃对话 ID）
- 原子写入：写 .tmp → rename
- `update()` 部分更新 + 立即 save

### feishu_client.py
- 单类 FeishuClient，async httpx
- Token 管理：双检锁模式（_token_lock），过期前 5 分钟刷新
- _request() 透明重试：401 / code=99991663 → 刷新 token + 重试一次
- 卡片构建：interactive schema 2.0，markdown 元素
- 文件上传：file_type 仅 pdf/doc/xls/ppt/stream，中文名 sanitize
- fetch_document()：docx → raw_content API；wiki → get_node → obj_token → raw_content API
- extract_text()：去除 @mention，支持 HTTP 回调和 WS 长连接两种格式

### claude_runner.py（最重要，别轻易改）
- `run_claude()` 启动子进程，构建 stream-json 命令
- 命令行参数：`-p --input-format stream-json --output-format stream-json --verbose --dangerously-skip-permissions`
- 子进程启动重试 3 次（Python 3.8 create_subprocess_exec 竞态）
- `IS_SANDBOX=1` 环境变量，pop CLAUDECODE 防嵌套

**双协程 stdin 管理模式（核心设计）**：
```
_stdin_writer:            _stdin_close_checker:
  queue.get() 阻塞等待       result_event.wait(timeout=600s)
  → new_round 通知 UI        → queue.empty()?
  → proc.stdin.write()       → Yes: sentinel → close stdin
  → 循环                     → No: 继续等待下一个 result
```
- new_round 事件在 stdin 写入**之前**发送，避免 UI 竞态
- RESULT_TIMEOUT=600s，超时 → timeout_error 事件 → close stdin
- 单轮模式（message_queue=None）：写完立即 close stdin

**stream-json 解析**：
- stdout 逐行 JSONL，6 种事件类型：assistant(text/tool_use)、user(tool_result)、result、system(init)
- stderr 独立协程读取，检测网络错误关键词 → 通知 UI
- subprocess transport 手动 close 释放 fd

### stream_handler.py
- `run_claude_and_stream()` 是事件回调的入口
- 卡片流式：buf 累积文本 → throttle_update（5s 间隔） → PATCH 更新卡片
- 工具调用：text_started 标记切换 → force push 旧卡片 → 新卡片显示工具状态
- result：保存历史、记录花费、发送推送通知

**节流规则**：
- 中间文本：最少 5s 间隔
- 工具切换/result：force=True 立即推
- update_card 失败 → 自动 fallback reply_message

**思考计时器**（_thinking_ticker，每 5s）：
- 无输出 < 60s：动画 + 计时
- 60-120s："复杂任务处理中"
- > 120s：警告可能网络异常
- 工具执行 ≥ 15s：追加耗时显示

**_truncate_for_card**：超 4000 字符 → 保存 workspace/output_{ts}.md + 自动清理旧文件（保留 20 个）

### main.py
**Bot 类**：单例，持有 feishu client、Claude 进程引用、消息队列、状态机

**命令路由**：EXACT dict（无参）+ PREFIX dict（有参），避免 if/elif 链
- /list、/stop、/start、/status、/cost、/help 等
- /new、/rename、/switch、/del、/view、/history

**休眠/唤醒**：
- 启动默认休眠（_idle=True），10s 轮询
- 休眠态仅响应 /start、/status、/cost、/help
- /start 激活 → 2s 轮询
- 30min 无消息自动休眠，**Claude 运行中不休眠**（上次迭代刚修）

**去重**：`_handle_message` 入口 `last_message_ts + last_message_id` 双重判断，在**第一个 await 之前**设置（防止协程切换导致重复处理）

**多轮消息**：Claude 运行中收到新消息 → `queue.put()` → `_stdin_writer` 消费，不 kill 进程

**飞书文档检测**：正则匹配 URL → fetch_document → 拼接 prompt。拉取失败显示权限提示，不发空请求

**/view 安全**：
- 定义 ALLOWED_ROOTS 白名单
- `resolve()` 消除 `../`
- `path.relative_to(root)` 校验，越界拒绝

### conversations.py
- Conversation dataclass：id(8位短UUID)、title、session_id、workspace、时间戳
- ConversationStore 单例，持久化 conversations.json（原子写入）
- list_all() 按 updated_at 倒序
- delete() 自动切换到最近的对话
- format_list() 显示 💾(有上下文)/🆕(新对话)/⚠️(丢失)，实际检查磁盘文件

### history_store.py
- 每对话一个 JSONL：`data/history/{conv_id}.jsonl`
- append() 追加 + 截断检查（行数 > 1000 或大小 > 4MB → 保留最近 500 行）
- import_from_claude()：/switch 时自动导入
  - content 支持字符串和列表两种格式
  - 跳过 parentUuid/isSidechain（子代理消息）
  - 跳过 "Request timed out" 占位

### cost_tracker.py（最新模块）
- 记录：result 事件 → `record(conv_id, cost_usd)` → `data/costs.jsonl`
- 查询：`query()` 返回 today/month/total
- 导入：`import_history()` 每次启动扫描 session 文件，按 (timestamp, cost_usd) 去重
- 注意：Claude session JSONL 不保存 result 事件，历史花费无法恢复

## 数据流完整链路

```
用户飞书发消息
  → Bot._poll_loop() 2s 轮询 get_messages()
  → _handle_message()
    → 去重 (last_message_ts + message_id)
    → 命令路由 (EXACT/PREFIX dict)
    → 文档检测 (regex feishu.cn/docx|wiki → fetch_document → 拼接到 prompt)
    → 保存用户消息到 history_store
    → 检查 Claude 是否运行中：
      - 是 → queue.put() → _stdin_writer 消费
      - 否 → stop_claude() 清理 → 创建 queue → reply_message("正在思考")
           → asyncio.create_task(run_claude_and_stream())
  → run_claude() 启动子进程
    → stdin: {"type":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]}}
    → stdout: 逐行 JSONL
    → _read_stdout() 解析 6 种事件 → on_event() 回调
  → stream_handler.on_event()
    → text → buf.append → throttle_update → update_card()
    → tool_call → force push 旧卡片 → reply_message 新卡片 → tool_log 记录
    → result → 保存历史 → 记录花费 → push 通知 → clear buf
```

## 关键约定和陷阱

1. **不要乱改 claude_runner.py 的双协程逻辑**。那个模式经过多次重构才稳定，竞态条件非常微妙。
2. **去重必须在第一个 await 之前**。如果先发 HTTP 请求再标记，asyncio 协程切换会导致同一条消息被处理多次。
3. **原子写入**：所有持久化文件用 ".tmp → rename"，不要直接写。
4. **new_round 事件在 stdin write 之前发**。如果反过来，_read_stdout 可能先读到输出并附加到 buf，然后 new_round 清空 buf，导致输出丢失。
5. **不支持图片**。DeepSeek API 当前不支持多模态，不要尝试添加。
6. **session JSONL 不存 result 事件**。cost_usd 无法从历史恢复，只能实时记录。
7. **模型切换会破坏 --resume**。不同模型的上下文格式不兼容。
8. **飞书 token 刷新是异步的**。_token_lock 防止并发刷新，双重检查模式。
9. **不要用 WebSocket**。REST 轮询可靠性远高于 WS 长连接，这是经验教训。
10. **subprocess transport 需要手动 close**。否则 fd 泄漏。

## 配置一览

所有可配参数：`src/config.py` → Config dataclass

| 参数 | 默认值 | 说明 |
|------|--------|------|
| POLL_INTERVAL | 2.0s | 活跃态轮询间隔 |
| IDLE_POLL_INTERVAL | 10.0s | 休眠态轮询间隔 |
| AUTO_IDLE_SEC | 1800s | 无消息自动休眠 |
| RESULT_TIMEOUT | 600s | 单轮超时 |
| THROTTLE_INTERVAL | 5.0s | 卡片更新节流 |
| CARD_MAX_CHARS | 4000 | 卡片截断阈值 |
| HISTORY_FILE_MAX_LINES | 500 | 历史行数上限 |
| HISTORY_FILE_MAX_BYTES | 2MB | 历史大小上限 |

## 测试

```bash
python3 -m pytest tests/ -v
```

35 个用例，覆盖：并发队列、超长文本、状态损坏、对话 CRUD、历史文件、stream-json 构建、命令注册、配置完整性。

测试用 tempfile 隔离数据目录，fixture 保存/恢复单例状态，防止污染运行时数据。

## 开发规范（请遵守）

- 零新依赖原则。当前唯一外部依赖是 httpx。加新库需要充分理由。
- 所有 print() 已改成 logging。新代码用 `log = logging.getLogger(__name__)`。
- 类型注解使用 `from __future__ import annotations`。
- 命令路由：EXACT dict（无参）和 PREFIX dict（有参），不写 if/elif 链。
- 飞书卡片 schema 2.0，markdown 元素。
- commit 用中文消息，Co-Authored-By: Claude <noreply@anthropic.com>。
- 保持对个人用户的定位。不做多用户、不做 Web Dashboard、不做复杂权限。

## 已知可扩展但未实现的方向

- `/retry` 命令：重新发送上一条用户消息
- `/export` 命令：导出当前对话为 Markdown
- 连续 poll error 阈值告警
- Bot 启动时发送飞书通知
- 图片支持（等 DeepSeek 支持多模态）
- systemd service（当前用 shell 看门狗已足够）

## 项目状态

- ✅ 功能完整，无已知 bug
- ✅ 35 测试全过
- ✅ 连续稳定运行 21 天无崩溃
- ✅ Apache 2.0 开源
- ✅ 中英文文档齐全
- ✅ 隐私已审计，无凭证泄露
- ✅ 可归档

---

**请在此基础上继续开发。不要重写，不要改变架构风格。小步迭代，保持简洁。**
