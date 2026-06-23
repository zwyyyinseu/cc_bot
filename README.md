# cc_bot — 飞书 Claude Code 移动端助手

在飞书手机上发消息，操控服务器上的 Claude Code 执行任务，结果流式回传。出差、躺床上、不在工位也能写代码。

## 能做什么

- 手机上发消息 → 服务器 Claude CLI 执行 → 实时卡片流式回传
- 多轮连续对话，Claude 进程保持存活，上下文不丢失
- 多对话管理（创建/切换/删除/重命名）
- 对话历史回溯（自动从 Claude session 导入）
- 休眠/唤醒（不用时不占资源）
- 长输出自动保存文件 + 发送到飞书（手机端直接打开预览）

## 项目结构

```
cc_bot/
├── src/                    # 源码
│   ├── main.py             # 入口：消息路由 + 状态机
│   ├── claude_runner.py    # Claude CLI 子进程管理（双协程 stdin）
│   ├── stream_handler.py   # Claude 输出 → 飞书卡片流式
│   ├── feishu_client.py    # 飞书 REST API 封装
│   ├── conversations.py    # 多对话管理
│   ├── history_store.py    # 历史持久化
│   ├── state.py            # 全局状态
│   └── config.py           # 配置管理
├── tests/                  # 测试（35 个用例）
├── scripts/                # 运维脚本
│   ├── start.sh            # 启动（带看门狗自恢复）
│   ├── stop.sh             # 停止
│   └── health.sh           # 健康检查
├── docs/                   # 文档
├── .env.example            # 配置模板
└── .gitignore
```

## 快速开始

### 1. 环境要求

- Python 3.11+
- Claude CLI（`npm install -g @anthropic-ai/claude-code` 或任意兼容 CLI）
- 飞书开放平台自建应用

### 2. 飞书应用配置

1. [飞书开放平台](https://open.feishu.cn) → 创建自建应用
2. 权限管理 → 添加权限：
   - `im:message` — 获取消息
   - `im:message:send_as_bot` — 发送消息
   - `im:resource:upload` — 上传文件（可选，用于文件阅读功能）
3. 安全设置 → 添加机器人
4. 发布应用

### 3. 安装

```bash
git clone git@github.com:zwyyyinseu/cc_bot.git
cd cc_bot
cp .env.example .env
# 编辑 .env，填入你的飞书凭证
vim .env
```

### 4. 配置 `.env`

```bash
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_OPEN_ID=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# CLAUDE_BIN=claude  # 可选，默认自动查找
```

### 5. 启动

```bash
bash scripts/start.sh
bash scripts/health.sh   # 检查状态
```

## 命令列表

| 命令 | 说明 |
|------|------|
| `/start` | 唤醒 Bot |
| `/stop` | 休眠 Bot |
| `/new <标题>` | 创建新对话 |
| `/rename <名字>` | 重命名当前对话 |
| `/list` | 列出所有对话 |
| `/switch <序号>` | 切换对话 |
| `/del <序号>` | 删除对话 |
| `/history [N]` | 查看历史对话 |
| `/view <路径>` | 查看文件（发送到飞书） |
| `/status` | 查看运行状态 |
| `/help` | 帮助信息 |

## 运行测试

```bash
python3 -m pytest tests/ -v
```

## 技术栈

- Python 3.11+ / asyncio
- Claude CLI stream-json 协议
- 飞书 REST API (schema 2.0 卡片)
- 零外部依赖（仅 httpx）

## License

MIT
