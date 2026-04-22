# OpenClaw migration

**Status:** Paused — waiting on upstream fix in OpenClaw 2026.4.19+.

## Context

The WhatsApp path goes: WhatsApp → OpenClaw (on VPS) → reverse SSH tunnel → `whatsapp_bridge.py` (FastAPI OpenAI-compat) → Claude Agent SDK. Two capabilities were broken at different times on different OpenClaw releases.

### 2026-04 CLI media bug (now fixed)

On OpenClaw 2026.4.9, `openclaw message send --media <path-or-url>` silently dropped images — it accepted the argument, returned a successful messageId, but no image ever reached the recipient. Verified with local paths, `file://` URLs, and public HTTPS URLs. Gateway send returned in ~15 ms, far too fast for a real media upload.

Root cause: [issue #64478](https://github.com/openclaw/openclaw/issues/64478) — gateway-side WhatsApp send missed the `mediaLocalRoots` fix from an earlier security patch. Fixed by [PR #64492](https://github.com/openclaw/openclaw/pull/64492), shipped in **2026.4.10**.

**Upgraded to 2026.4.14** on 2026-04-20. Scratch-tested the fix end-to-end (pair, render PNG, `message send --media` → image arrived). CLI media is now working.

### Remaining blocker — LLM-generated media in WhatsApp replies

OpenClaw's intended mechanism for LLM-generated media is inline `MEDIA:/abs/path` markers in the LLM reply that OpenClaw auto-attaches — tracked as [issue #66635](https://github.com/openclaw/openclaw/issues/66635), still failing in 2026.4.14 for LLM-path replies (works for CLI). No confirmed target release; previously guessed 2026.4.19+ without evidence.

### Clarification on sender identity (2026-04-22)

Earlier investigation had claimed OpenClaw doesn't pass sender identity to the LLM backend. That was wrong. Identity **is** passed — just not in the OpenAI `user` field or HTTP headers. It's embedded in the **content of each user message** as a structured metadata block, before the actual user text:

```
Conversation info (untrusted metadata):
{
  "sender_id": "+14084258476",
  "sender": "Bala",
  "conversation_label": "<group JID or DM peer>",
  "is_group_chat": true,
  "was_mentioned": true,
  "group_subject": "<group title>",
  "history_count": 14,
  "timestamp": "..."
}

Sender (untrusted metadata):
{
  "label": "Bala (+14084258476)",
  "id": "+14084258476",
  "name": "Bala",
  "e164": "+14084258476"
}

<actual user message here>
```

Consequence: our bridge can parse `sender_id` from the latest user message and use it directly for memory keying, access logging, or anything else that needs per-sender identity. No WAHA migration required for the "remember Ayaps vs Boss" use case. The media-send blocker is unaffected by this finding — it's a separate routing problem inside OpenClaw.

## Decision matrix considered

| Option | WhatsApp speed | LLM → media | Cost | Effort |
|---|---|---|---|---|
| Stay on OpenClaw | Fast (Baileys native) | Blocked pending 2026.4.19+ | Free | 0 |
| WAHA Free (WEBJS/Puppeteer) | Slow (per padhu0626 repro) | Not available in free tier | Free | Medium |
| WAHA Plus (~$19/mo donation) | Fast (NOWEB/GOWS) | Works | $19/mo | Medium |
| Extend our own `baileys/gateway.js` | Fast (under our control) | Works | Free | ~1 afternoon |

WAHA Free is ruled out — loses on both speed and media. If we migrate, it's WAHA Plus or DIY Baileys.

## Decision

**Option 1 — wait for LLM-path media fix, continue on OpenClaw for everything else.** On 2026.4.14 text replies and CLI-invoked outbound media work. Per-sender identity works via metadata parsing (see clarification above), so the adjacent "agents souls and memory" project is unblocked without WAHA. When the LLM-path media fix lands upstream, verify it works and add a Sonic prompt line that allows `MEDIA:` markers on explicit user request. If it doesn't land within a timeframe Boss finds acceptable, revisit WAHA migration.

## Related open concern

Claude auth is subscription OAuth (`CLAUDE_CODE_USE_SUBSCRIPTION=1`). Anthropic's published ToS (code.claude.com/docs/en/authentication, /headless) prohibits using subscription OAuth for agents serving other users — the Agent SDK is explicitly named. Active enforcement began early 2026. Migration to `ANTHROPIC_API_KEY` is a two-line `.env` change; deferred by choice after risk flagged.
