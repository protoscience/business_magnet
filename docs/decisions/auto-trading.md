# Auto trading for SuperSonic

**Status:** Active build — Phase 1 in progress on branch `auto-trading`.

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
| 0 | This decision doc + worktree setup | 30 min | ✓ done |
| 1 | `tools/auto_trade.py` (execute_order + rails + kill-switch + audit log) + `logs/trades.db` + `auto_place_order` MCP tool + SuperSonic soul/memory pulled from souls-memory | 1 day | in progress |
| 2 | Mode B — LLM scan. Timer-driven, dry-run first. Watchlist injection with static + dynamic. | 1 day | |
| 3 | Mode C — Strategy harness. Base class, registry, one canned strategy. | 1 day | |
| 4 | Mode D — Webhook. `trading_webhook.py` on port 4001 with shared-secret auth. | half day | |
| 5 | Grafana dashboard: open positions, daily P&L, trade log, win rate by source. | half day | |
| 6 | Discord push-notify on every trade + daily summary. | half day | |

## Dry-run mode

Phase 1 ships with `AUTO_TRADE_DRY_RUN=true` default. Every `execute_order` call logs what it would have done (full order intent + risk-rail results) to `logs/trades.db` with `status='dry_run'`, but does NOT submit to Alpaca. Flip to `false` in `.env` when comfortable.

## Rollback

Every phase is a single revert. Phase 1 is fully additive (new files + additions to existing) — reverting leaves Sonic + SuperSonic's current behavior unchanged. Memory files in `logs/memory/supersonic/` become harmless orphans; `logs/trades.db` too.

## Open questions to revisit post-Phase 1

- Should Phase 2 LLM scan be a *separate* Claude Agent SDK session (not piggybacking SuperSonic's Discord session) to avoid mixing user-chat context with algorithmic decisions? Leaning yes.
- Bracket stops: submit as native Alpaca bracket orders (attached at fill) vs. monitor-and-close loop in our code? Native is simpler, use unless Alpaca rejects for our whitelist symbols.
- Discord notification channel: push to the existing SuperSonic server? Same channel as chat, or dedicated `#auto-trade` channel? Boss decides at Phase 6.
