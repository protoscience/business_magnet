# Auto trading for SuperSonic

**Status:** Phase 1 deployed. Phase 2 ready on branch `auto-trading-phase2`.

## Progress

- **Phase 1** — deployed to `main` as commit `3bac51d` on 2026-04-24. `auto_place_order` tool callable from Discord, rails + audit log + dry-run working end-to-end. Smoke-confirmed with a manual `@SuperSonic auto_place_order AAPL...` call that cleanly hit the time-window rail.
- **Phase 2** — full scope (LLM scan + dynamic watchlist + reliability) ready on branch `auto-trading-phase2`. Awaiting deploy. See §"Phase 2 details" below.

## Scope

SuperSonic (Discord only) gains auto-execution of Alpaca paper-trading orders from three trigger sources combined:

- **B — LLM-discretionary scans**: systemd timer calls SuperSonic every N minutes during RTH; Claude analyzes a watchlist and emits trade intents.
- **C — Rules-based strategies**: scheduled Python strategy ticks that compute entries/exits from bars + signals.
- **D — External webhooks**: FastAPI endpoint on a new port (e.g. 4001); TradingView / custom screeners POST signals with a shared secret.

All three triggers funnel into **one** gated execution layer (`tools/auto_trade.py`) that enforces risk rails before any order reaches Alpaca.

Sonic (WhatsApp) is **explicitly not touched** — no trading tools in `RESEARCH_TOOLS`, no auto-trade code imported by `whatsapp_bridge.py`, no shared state with SuperSonic's trading. Sonic remains research-only for friends.

## Watchlist

**Static (12)**: SPY, QQQ, TQQQ, NVDA, AMD, AMZN, AAPL, MSFT, META, GOOGL, ARM, INTC.

**Dynamic (Phase 2 adds these per-scan)**:
- News-trending tickers — heuristic via SearXNG (search trending finance terms, LLM extracts tickers).
- Earnings-this-week tickers — Finnhub free tier (60 req/min, earnings calendar endpoint).

Dynamic candidates filtered through the same whitelist check: only symbols from the static list OR successfully resolved by dynamic sources can trade. No random sneak-through.

## Risk rails — enforced in code, not prompt

Every trade through every mode must pass:

| Rail | Value |
|---|---|
| Paper-only hardcoded | `ALPACA_PAPER=true` required; abort otherwise |
| Position size | **$1,000 per trade** (fixed dollar, not %) |
| Max open positions | 5 |
| Daily loss kill-switch | –3% equity vs previous close → block new orders rest of day |
| Symbol whitelist | Static 12 + dynamic resolved set only |
| Time window | No orders first 5 min of RTH; no new longs last 15 min |
| Kill-switch file | `logs/kill-switch` exists → all blocked |
| Per-strategy daily max trades | 10 |

### Stop-loss policy

| Symbol class | Auto stop-loss |
|---|---|
| Mag7 (AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA) | **None** — rides without auto-stop |
| All others (AMD, ARM, INTC, SPY, QQQ, TQQQ, dynamic adds) | **–5%** bracket stop attached at fill |

## SuperSonic soul + memory (scoped-in from parked `souls-memory` branch)

Selectively merge:
- `tools/memory.py` (generic memory module)
- `prompts/supersonic_soul.md` (SuperSonic's personality)
- SuperSonic-side wiring in `agent_core.py` and `discord_bot.py`

Excluded:
- `prompts/sonic_soul.md`
- Sonic-side wiring in `whatsapp_bridge.py`

Sonic's soul/memory stays parked until the user-consent question is resolved (see `docs/decisions/agent-souls-and-memory.md`).

SuperSonic memory will help auto-trading context — remembers Boss's risk tolerance, prior setups he liked, positions he's watching, stop-loss preferences. Keyed by `discord:1474877861524537416` (Boss's Discord ID).

## Phased plan

| Phase | What | Est | Status |
|---|---|---|---|
| 0 | Decision doc + worktree setup | 30 min | ✓ done |
| 1 | `tools/auto_trade.py` (execute_order + rails + kill-switch + audit log) + `logs/trades.db` + `auto_place_order` MCP tool + SuperSonic soul/memory pulled from souls-memory | 1 day | ✓ deployed 2026-04-24 |
| 2 | Mode B — LLM scan + dynamic watchlist + DB reliability hardening | 1 day | ✓ built, awaiting deploy |
| 3 | Mode C — Strategy harness. Base class, registry, one canned strategy. | 1 day | queued |
| 4 | Mode D — Webhook. `trading_webhook.py` on port 4001 with shared-secret auth. | half day | queued |
| 5 | Grafana dashboard: open positions, daily P&L, trade log, win rate by source. | half day | queued |
| 6 | Discord push-notify on every trade + daily summary. | half day | queued |

## Phase 2 details (what shipped)

**`tools/auto_scan.py`** — one-shot scan, timer-invoked. Flow:
1. Market-hours guard via `TradingClient.get_clock()` — handles holidays + half-days, no static calendar needed.
2. Kill-switch file check.
3. Aggregate context: quotes + 20-day bars for static 12, account equity + positions + pnl_today, news/earnings snippets via `ticker_feeds`.
4. Call Claude with `ClaudeAgentOptions(mcp_servers={}, allowed_tools=[])` — no tools, pure text-in / JSON-out. Saves tokens vs an agentic tool loop.
5. Parse JSON `{entries, exits, passed_on, note}`. Tolerates stray markdown fences.
6. Route each intent through `auto_trade.execute_order`. Same 9 rails as Phase 1, same audit log, same dry-run flag.

**`prompts/auto_scan_prompt.md`** — scan-mode system prompt, separate from SuperSonic's interactive persona. Procedural, JSON-only output, high entry bar ("default correct answer is do nothing"), dynamic candidates allowed with clear catalysts.

**`tools/ticker_feeds.py`** — SearXNG-based news + earnings snippet fetcher. Picked SearXNG over Finnhub free tier because (a) no external API-key signup, (b) already deployed, (c) raw snippets go into prompt — the LLM picks tickers, no brittle regex. Swap to Finnhub via `fetch_earnings_snippets` replacement if quality degrades.

**`trading-auto-scan.{service,timer}`** — fires 25× per trading day at 09:35, 09:50, … 15:35, 15:45 in `America/New_York`. Mon–Fri only via systemd's `OnCalendar=Mon..Fri` restriction. Holidays still handled by the in-script Alpaca clock guard.

### Phase 2 reliability hardening (also shipped)

**`tools/db_backup.py`** — nightly integrity check + local tar + rsync to VPS + rotation. Runs at 03:15 local (staggered from cost-rollup at 03:00). Knobs: `DB_BACKUP_DIR`, `DB_BACKUP_REMOTE`, `DB_BACKUP_REMOTE_DIR`, `DB_BACKUP_KEEP`. Corrupt DB → aborts + preserves last-good remote copy.

**`trading-db-backup.{service,timer}`** — installed alongside scan timer.

## Deploy order for Phase 2

```bash
cd /home/eswar/claude/trading-agent
git fetch origin auto-trading-phase2
git merge --ff-only origin/auto-trading-phase2
git push origin main

# Install the two new timers
systemctl --user daemon-reload
systemctl --user enable --now trading-db-backup.timer
systemctl --user enable --now trading-auto-scan.timer

# Verify
systemctl --user list-timers trading-auto-scan.timer trading-db-backup.timer --no-pager
```

First trading-hour scan will fire at the next 15-minute boundary past 09:35 ET on Monday. First backup at 03:15 tonight.

## Dry-run mode

Phase 1 ships with `AUTO_TRADE_DRY_RUN=true` default. Every `execute_order` call logs what it would have done (full order intent + risk-rail results) to `logs/trades.db` with `status='dry_run'`, but does NOT submit to Alpaca. Flip to `false` in `.env` when comfortable.

## Rollback

Every phase is a single revert. Phase 1 is fully additive (new files + additions to existing) — reverting leaves Sonic + SuperSonic's current behavior unchanged. Memory files in `logs/memory/supersonic/` become harmless orphans; `logs/trades.db` too.

## Open questions to revisit post-Phase 1

- Should Phase 2 LLM scan be a *separate* Claude Agent SDK session (not piggybacking SuperSonic's Discord session) to avoid mixing user-chat context with algorithmic decisions? Leaning yes.
- Bracket stops: submit as native Alpaca bracket orders (attached at fill) vs. monitor-and-close loop in our code? Native is simpler, use unless Alpaca rejects for our whitelist symbols.
- Discord notification channel: push to the existing SuperSonic server? Same channel as chat, or dedicated `#auto-trade` channel? Boss decides at Phase 6.
