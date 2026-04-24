# agent_core

Reusable framework for Claude agents on **WhatsApp** and **Discord**.

`agent_core` provides:

- A FastAPI-based **WhatsApp bridge** (OpenAI-compatible chat-completions API,
  designed to sit behind [OpenClaw](https://github.com/openclaw) on a VPS).
- A **Discord bot runner** with private-bot guardrails (user allowlist,
  guild allowlist, auto-leave on join, confirmation UI for risky tool calls).
- Per-sender **memory** (date-stamped facts, TTL, cap, agent silos).
- Generic tools: web search (SearXNG), analysis-card image rendering,
  cost tracking (per-turn, per-sender, daily roll-ups, dashboard).
- A clean library contract: applications register their own
  `@tool`-decorated functions and a system prompt; the framework handles
  session lifecycle, auth, image-marker stripping, confirmation flow,
  and cost logging.

Agents themselves (Sonic the WhatsApp researcher, SuperSonic the Discord
trader) live in **separate application repos** that consume `agent_core`.
This repo intentionally has no domain logic.

## Install

```bash
pip install -e ../agent_core    # editable, from a sibling app repo
```

## Minimal example

```python
# my_app/discord_bot.py
from agent_core import build_options, run_discord
from agent_core.tools import remember, recall_about_me, search_web
from claude_agent_sdk import tool

@tool("ping", "Reply with pong.", {})
async def ping(args):
    return {"content": [{"type": "text", "text": "pong"}]}

ALL_TOOLS = [ping, remember, recall_about_me, search_web]

def build_my_options(*, sender_key=None, sender_name=None):
    return build_options(
        system_prompt="You are a helpful assistant.",
        tools=ALL_TOOLS,
        agent_name="my-app",
        sender_key=sender_key,
        sender_name=sender_name,
    )

if __name__ == "__main__":
    run_discord(build_my_options)
```

## Status

Pre-1.0. The library contract is being shaken out by `trading_core`
(private; Sonic + SuperSonic). API may break between minor versions
until 1.0.
