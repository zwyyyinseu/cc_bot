# cc_bot — Claude Code Mobile Assistant via Feishu

Control Claude Code on your server from your phone through Feishu (Lark) messaging. Stream results back in real-time. Write code from bed, on the road, anywhere.

## Features

- Send messages from Feishu → Claude CLI executes on server → real-time card streaming
- Multi-turn conversation with context preserved (Claude process kept alive)
- Multiple conversation management (create / switch / delete / rename)
- Conversation history with automatic import from Claude sessions
- Idle / wake modes to save resources when not in use
- Long output auto-saved to files and sent to Feishu chat (preview on mobile)
- Subagent support: cheap model for parallel search/read tasks, powerful model for main reasoning

## Project Structure

```
cc_bot/
├── src/                    # Source code
│   ├── main.py             # Entry: message routing + state machine
│   ├── claude_runner.py    # Claude CLI subprocess management (dual-coroutine stdin)
│   ├── stream_handler.py   # Claude output → Feishu card streaming
│   ├── feishu_client.py    # Feishu REST API client
│   ├── conversations.py    # Multi-conversation management
│   ├── history_store.py    # History persistence (JSONL)
│   ├── state.py            # Global state persistence
│   └── config.py           # Configuration management
├── tests/                  # Tests (35 cases)
├── scripts/                # Ops scripts
│   ├── start.sh            # Start (with watchdog auto-recovery)
│   ├── stop.sh             # Stop
│   └── health.sh           # Health check
├── docs/                   # Documentation (Chinese)
├── .env.example            # Environment template
└── .gitignore
```

## Quick Start

### 1. Requirements

- Python 3.11+
- Claude CLI (`npm install -g @anthropic-ai/claude-code` or compatible CLI)
- A Feishu (Lark) self-built app

### 2. Feishu App Setup

1. [Feishu Open Platform](https://open.feishu.cn) → Create a self-built app
2. Permission Management → Add permissions:
   - `im:message` — receive messages
   - `im:message:send_as_bot` — send messages
   - `im:resource:upload` — upload files (optional, for file reading)
3. Security Settings → Add bot capability
4. Publish the app

### 3. Installation

```bash
git clone git@github.com:zwyyyinseu/cc_bot.git
cd cc_bot
cp .env.example .env
# Edit .env with your Feishu credentials
vim .env
```

### 4. Configure `.env`

```bash
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_OPEN_ID=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# CLAUDE_BIN=claude  # optional, auto-detected
```

### 5. Start

```bash
bash scripts/start.sh
bash scripts/health.sh   # verify status
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Wake up bot |
| `/stop` | Put bot to sleep |
| `/new <title>` | Create a new conversation |
| `/rename <name>` | Rename current conversation |
| `/list` | List all conversations |
| `/switch <index>` | Switch to another conversation |
| `/del <index>` | Delete a conversation |
| `/history [N]` | Show recent conversation history |
| `/view <path>` | View a file (sent to chat) |
| `/status` | Show bot running status |
| `/help` | Show help |

## How It Works

```
┌──────────┐   poll every 2s    ┌──────────┐   stream-json    ┌──────────┐
│  Feishu  │ ←──────────────── │  cc_bot   │ ←────────────── │  Claude  │
│  (phone) │   REST API         │  (server) │   stdin/stdout  │   CLI    │
│          │ ─────────────────→ │           │ ───────────────→ │          │
│  message │   card streaming   │           │   tool calls     │          │
└──────────┘                   └──────────┘                  └──────────┘
```

- **Polling**: Fetch messages via Feishu REST API every 2s (active) or 10s (idle)
- **Streaming**: Claude output parsed from stream-json format, pushed to Feishu interactive cards
- **Multi-turn**: Claude process stdin stays open between messages — new messages queued and written immediately, no cold start
- **Watchdog**: `start.sh` auto-restarts on crash (up to 10 times/day), with lockfile to prevent duplicates

## Running Tests

```bash
python3 -m pytest tests/ -v
```

## Tech Stack

- Python 3.11+ / asyncio
- Claude CLI stream-json protocol
- Feishu REST API (interactive cards schema 2.0)
- Zero external dependencies (only httpx)

## License

MIT
