# Business Magnet - AI Trading Research Agent

A Claude-powered trading research agent with Discord and WhatsApp interfaces. Uses Alpaca for paper trading and market data, SearXNG for web search, and generates rich visual analysis cards.

## Architecture

```
Discord ──► Discord Bot (full trading + research)
                │
                ├──► Claude Agent SDK ──► Alpaca (paper trading)
                │                    ──► SearXNG (web search)
                │                    ──► Playwright (image gen)
                │
WhatsApp ──► OpenClaw (VPS) ──► SSH tunnel ──► WA Bridge (research only)
                                                    │
                                                    ├──► Claude Agent SDK ──► Alpaca (quotes/options data)
                                                                         ──► SearXNG (web search)
```

**Discord**: Full access — stock quotes, bars, options chains, web search, analysis cards, price charts, and paper order placement (with button confirmation).

**WhatsApp**: Research only — no account info, no positions, no order placement. Designed for group discussions.

## Prerequisites

- Python 3.12+
- Node.js 22+ (for OpenClaw on VPS)
- Docker (for SearXNG)
- Claude Code CLI with active subscription (`claude` must be logged in)
- [Alpaca](https://alpaca.markets/) paper trading account
- [Discord](https://discord.com/developers/applications) bot token

## Quick Setup

### 1. Clone and configure

```bash
git clone git@github.com:protoscience/business_magnet.git
cd business_magnet

cp .env.example .env
# Fill in your API keys in .env
```

### 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 3. SearXNG (web search)

```bash
sudo docker compose up -d
```

This starts SearXNG on `127.0.0.1:8080` (localhost only).

### 4. Run Discord bot (manual)

```bash
source .venv/bin/activate
python discord_bot.py
```

### 5. Run as systemd services (recommended)

Create these service files in `~/.config/systemd/user/`:

**trading-discord.service**
```ini
[Unit]
Description=Trading Agent - Discord Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/business_magnet
EnvironmentFile=/path/to/business_magnet/.env
ExecStart=/path/to/business_magnet/.venv/bin/python discord_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**trading-wa-bridge.service**
```ini
[Unit]
Description=Trading Agent - WhatsApp Bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/business_magnet
EnvironmentFile=/path/to/business_magnet/.env
Environment=BRIDGE_TOKEN=<generate-with-python3 -c "import secrets; print(secrets.token_hex(24))">
Environment=BRIDGE_PORT=4000
ExecStart=/path/to/business_magnet/.venv/bin/python whatsapp_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**trading-ssh-tunnel.service** (reverse tunnel to VPS for WhatsApp)
```ini
[Unit]
Description=Trading Agent - Reverse SSH Tunnel to VPS
After=network-online.target

[Service]
Type=simple
Environment=AUTOSSH_GATETIME=0
ExecStart=/usr/bin/autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -R 4000:127.0.0.1:4000 user@your-vps-host
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

**trading-watcher.service** (auto-restart on code changes)
```ini
[Unit]
Description=Trading Agent - File Watcher
After=trading-discord.service trading-wa-bridge.service

[Service]
Type=simple
ExecStart=/path/to/business_magnet/watch-restart.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Enable and start:
```bash
# Allow services to run after logout
sudo loginctl enable-linger $USER

# Install autossh and inotify-tools
sudo apt install autossh inotify-tools

systemctl --user daemon-reload
systemctl --user enable --now trading-discord trading-wa-bridge trading-ssh-tunnel trading-watcher
```

## WhatsApp Setup (via OpenClaw)

WhatsApp integration uses [OpenClaw](https://github.com/openclaw/openclaw) as a gateway on a VPS.

### On the VPS:

1. Install OpenClaw:
   ```bash
   sudo npm install -g openclaw@2026.4.9
   ```

2. Configure WhatsApp channel and pair:
   ```bash
   openclaw configure
   openclaw channels login --channel whatsapp
   ```

3. Add the bridge model provider to `~/.openclaw/openclaw.json`:
   ```json
   {
     "env": {
       "BRIDGE_TOKEN": "<same-token-as-wa-bridge-service>"
     },
     "models": {
       "providers": {
         "litellm": {
           "baseUrl": "http://127.0.0.1:4000",
           "apiKey": "${BRIDGE_TOKEN}",
           "api": "openai-completions",
           "models": [{
             "id": "trading-agent",
             "name": "Trading Agent (Claude)",
             "reasoning": false,
             "input": ["text"],
             "contextWindow": 200000,
             "maxTokens": 4096
           }]
         }
       }
     },
     "agents": {
       "defaults": { "model": { "primary": "litellm/trading-agent" } },
       "list": [
         { "id": "main" },
         { "id": "trading", "name": "trading", "model": "litellm/trading-agent" }
       ]
     },
     "bindings": [
       { "agentId": "trading", "match": { "channel": "whatsapp" } }
     ],
     "channels": {
       "whatsapp": {
         "dmPolicy": "allowlist",
         "allowFrom": ["+1XXXXXXXXXX"],
         "groupPolicy": "allowlist",
         "groupAllowFrom": ["+1XXXXXXXXXX"],
         "groups": { "*": { "requireMention": true } },
         "enabled": true
       }
     }
   }
   ```

   **Group behavior:** With `groups."*".requireMention=true`, Sonic only responds in
   group chats when explicitly @-mentioned (native WhatsApp tap-to-mention).
   `groupPolicy: "allowlist"` plus `groupAllowFrom` further restricts *who* can
   trigger a reply — non-allowlisted senders are silently ignored even if they
   @-mention Sonic. DMs use `dmPolicy`/`allowFrom` independently. Per OpenClaw
   docs, replying to a Sonic message satisfies mention gating but does NOT
   bypass the sender allowlist.

4. Start the gateway:
   ```bash
   openclaw gateway
   ```

The reverse SSH tunnel (from your local machine) makes the bridge reachable at `127.0.0.1:4000` on the VPS. All traffic is SSH-encrypted.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ALPACA_API_KEY` | Yes | Alpaca paper trading API key |
| `ALPACA_SECRET_KEY` | Yes | Alpaca paper trading secret |
| `ALPACA_PAPER` | Yes | Must be `true` |
| `SEARXNG_URL` | Yes | SearXNG URL (default: `http://localhost:8080`) |
| `CLAUDE_CODE_USE_SUBSCRIPTION` | Yes | Set to `1` for subscription auth |
| `DISCORD_BOT_TOKEN` | Yes | Discord bot token |
| `DISCORD_ALLOWED_USER_IDS` | Yes | Comma-separated Discord user IDs |
| `DISCORD_ALLOWED_CHANNEL_IDS` | No | Comma-separated channel IDs (bot responds without @mention) |

## Security Notes

- All services bind to `127.0.0.1` (localhost only)
- SearXNG is not exposed to the internet
- WhatsApp bridge uses bearer token auth
- SSH tunnel provides encryption between local machine and VPS
- `.env` should be `chmod 600`
- WhatsApp channel is research-only (no trading tools)
- Discord order placement requires button confirmation
- WhatsApp access controlled via OpenClaw allowlist

## Useful Commands

```bash
# Check service status
systemctl --user status trading-discord trading-wa-bridge trading-ssh-tunnel trading-watcher

# Tail logs
journalctl --user -u trading-discord -f

# Manual restart
systemctl --user restart trading-discord

# Reset conversation in Discord
# Type /reset or !reset in Discord
```
