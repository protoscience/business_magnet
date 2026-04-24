"""Scheduled LLM-discretionary scan — Phase 2.

Runs on a systemd timer every 15 min during RTH (Mon-Fri, 09:35..15:45 ET).
Each invocation is a standalone shot:
  1. Guard: market-hours + kill-switch + weekend/holiday check
  2. Pre-aggregate: fetch quotes + recent bars for the static watchlist,
     news + earnings snippets, account + positions
  3. Call Claude with a scan-mode system prompt and the aggregated payload
     (no tools exposed — one-turn JSON output)
  4. Parse JSON decisions, pass each through auto_trade.execute_order
     (rails apply as normal; dry-run still honored via .env)
  5. Log everything to trades.db via trade_log

Invoke manually:
    python -m tools.auto_scan [--dry-run] [--force]

    --dry-run   overrides AUTO_TRADE_DRY_RUN=true for this run only
    --force     skip the market-hours guard (for testing)
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.client import TradingClient
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from tools import auto_trade, market_data, ticker_feeds, trade_log
from tools.auto_trade import STATIC_WATCHLIST

REPO = Path(__file__).resolve().parent.parent
SCAN_PROMPT_PATH = REPO / "prompts" / "auto_scan_prompt.md"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("auto-scan")


@dataclass
class ScanResult:
    status: str             # ok | skipped:<why> | error
    entries: int = 0
    exits: int = 0
    passed_on: int = 0
    note: str = ""


# ── Market-hours guard ─────────────────────────────────────────────────────

def _market_is_open(force: bool = False) -> tuple[bool, str]:
    """Use Alpaca's own clock — it knows about holidays + half-days."""
    if force:
        return True, "forced"
    try:
        client = TradingClient(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
            paper=True,
        )
        clock = client.get_clock()
        return bool(clock.is_open), ("open" if clock.is_open else "closed")
    except Exception as e:
        log.exception(f"clock fetch failed: {e}")
        return False, f"clock error: {e}"


def _kill_switch_active() -> bool:
    return auto_trade.KILL_SWITCH.exists()


# ── Pre-aggregation ────────────────────────────────────────────────────────

def _aggregate_static_context() -> dict:
    """Gather quotes + short bars for each static watchlist symbol.

    Failures on individual symbols don't abort the scan — missing data is
    acceptable; Claude works with what's available.
    """
    quotes: dict[str, dict] = {}
    bars: dict[str, list] = {}
    for sym in sorted(STATIC_WATCHLIST):
        try:
            quotes[sym] = market_data.get_latest_quote(sym)
        except Exception:
            quotes[sym] = {"error": "unavailable"}
        try:
            # 5-day bars for intraday context + 20-day bars to show trend
            bars[sym] = market_data.get_recent_bars(sym, days=20, timeframe="1Day")
        except Exception:
            bars[sym] = []
    return {"quotes": quotes, "bars": bars}


def _aggregate_account_state() -> dict:
    try:
        client = TradingClient(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
            paper=True,
        )
        account = client.get_account()
        positions = client.get_all_positions()
        equity = float(account.equity)
        last_equity = float(account.last_equity)
        pnl_pct = (equity - last_equity) / last_equity * 100.0 if last_equity else 0.0
        return {
            "equity": round(equity, 2),
            "pnl_today_pct": round(pnl_pct, 2),
            "buying_power": round(float(account.buying_power), 2),
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": int(float(p.qty)),
                    "avg_entry_price": round(float(p.avg_entry_price), 2),
                    "market_price": round(float(p.current_price), 2),
                    "unrealized_plpc": round(float(p.unrealized_plpc) * 100, 2),
                }
                for p in positions
            ],
        }
    except Exception as e:
        log.exception(f"account state fetch failed: {e}")
        return {"error": str(e)}


def _aggregate_timing() -> dict:
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    minutes_since_open = max(0, (now_et.hour - 9) * 60 + (now_et.minute - 30))
    minutes_until_close = max(0, (16 * 60) - (now_et.hour * 60 + now_et.minute))
    return {
        "now_et": now_et.strftime("%Y-%m-%d %H:%M %Z"),
        "minutes_since_open": minutes_since_open,
        "minutes_until_close": minutes_until_close,
    }


# ── Claude call ────────────────────────────────────────────────────────────

def _load_prompt() -> str:
    try:
        return SCAN_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        log.exception("scan prompt load failed; using minimal fallback")
        return "Return one JSON object: {entries:[], exits:[], passed_on:[], note:''}"


async def _call_claude(system_prompt: str, payload: dict) -> str:
    """One-shot Claude call with NO tools. Returns raw text."""
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={},
        allowed_tools=[],
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        user_msg = "PAYLOAD:\n```json\n" + json.dumps(payload, default=str, indent=2) + "\n```"
        await client.query(user_msg)
        text = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text += block.text
            elif isinstance(msg, ResultMessage):
                break
        return text
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _parse_decisions(text: str) -> dict:
    """Extract the JSON object. Tolerates stray markdown fences just in case."""
    # strip ```json fences if present
    stripped = re.sub(r"^```(?:json)?\s*\n?|\n?```\s*$", "", text.strip(), flags=re.MULTILINE)
    # find first {...} span
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in scan output: {text[:200]!r}")
    return json.loads(match.group(0))


# ── Execute decisions ──────────────────────────────────────────────────────

def _execute_intents(
    intents: list[dict],
    side: str,
    dynamic_set: set[str],
) -> int:
    """Run each intent through auto_trade.execute_order. Return count actually
    submitted or dry-run'd (i.e. not blocked/error)."""
    succeeded = 0
    for it in intents:
        symbol = str(it.get("symbol", "")).upper().strip()
        reason = str(it.get("reason", ""))[:200]
        if not symbol:
            continue
        intent = auto_trade.OrderIntent(
            symbol=symbol,
            side=side,
            source="llm-scan",
            reason=reason,
            dynamic_allowed=(symbol not in STATIC_WATCHLIST and symbol in dynamic_set),
        )
        result = auto_trade.execute_order(intent)
        log.info(
            f"{side} {symbol}: status={result.status} qty={result.qty} "
            f"stop={result.stop_loss_price} reason={result.reason[:120]}"
        )
        if result.status in ("submitted", "dry_run"):
            succeeded += 1
    return succeeded


def _dynamic_symbols(news: list[dict], earnings: list[dict]) -> set[str]:
    """Crude ticker harvest from snippet text — used only to populate the
    'dynamic_allowed' rail bypass. Quality control is the LLM's job; this
    just ensures the whitelist rail doesn't reject a symbol Claude justified.

    We accept 2-5 char uppercase tokens that aren't on a common-noun blocklist.
    """
    blocklist = {
        "NYSE", "NASDAQ", "SEC", "USA", "USD", "CEO", "CFO", "CTO", "COO",
        "IPO", "ETF", "API", "EPS", "EV", "P&L", "PE", "PS", "PB", "RSI",
        "MA", "SMA", "EMA", "CPI", "PCE", "GDP", "FED", "FOMC", "ECB", "YOY",
        "QOQ", "AI", "ML", "IT", "HR", "ER", "ERS", "US", "UK", "EU", "JP",
        "RTH", "ETH", "BTC", "ATH", "ATL", "LLC", "LTD", "INC", "CORP",
    }
    collected: set[str] = set()
    for s in (news or []) + (earnings or []):
        text = f"{s.get('title', '')} {s.get('content', '')}"
        for m in re.findall(r"\b([A-Z]{2,5})\b", text):
            if m in blocklist:
                continue
            collected.add(m)
    return collected


# ── Main entrypoint ────────────────────────────────────────────────────────

async def run_scan(force: bool = False) -> ScanResult:
    # Guard 1: market hours + holidays
    is_open, state = _market_is_open(force=force)
    if not is_open:
        log.info(f"skipping scan: market {state}")
        return ScanResult(status=f"skipped:market-{state}")

    # Guard 2: kill-switch
    if _kill_switch_active():
        log.warning("skipping scan: kill-switch file present")
        return ScanResult(status="skipped:kill-switch")

    # Aggregate
    log.info("aggregating scan context")
    static_ctx = _aggregate_static_context()
    account_ctx = _aggregate_account_state()
    timing_ctx = _aggregate_timing()
    news, earnings = await ticker_feeds.fetch_all()

    # Compose payload
    payload = {
        "timing": timing_ctx,
        "account": account_ctx,
        "static_watchlist": sorted(STATIC_WATCHLIST),
        "quotes": static_ctx["quotes"],
        "bars_recent": {k: v[-10:] for k, v in static_ctx["bars"].items()},
        "news_snippets": news,
        "earnings_snippets": earnings,
    }

    # Claude call
    prompt = _load_prompt()
    log.info("calling Claude scan prompt")
    try:
        raw = await _call_claude(prompt, payload)
    except Exception as e:
        log.exception(f"Claude call failed: {e}")
        trade_log.record(
            source="llm-scan", symbol="*", side="-", qty=0,
            order_type="-", status="error", reason=f"claude call: {e}",
        )
        return ScanResult(status="error")

    # Parse JSON
    try:
        decisions = _parse_decisions(raw)
    except Exception as e:
        log.exception(f"parse_decisions failed: raw[:400]={raw[:400]!r}")
        trade_log.record(
            source="llm-scan", symbol="*", side="-", qty=0,
            order_type="-", status="error", reason=f"parse: {e}",
        )
        return ScanResult(status="error")

    entries = decisions.get("entries", []) or []
    exits = decisions.get("exits", []) or []
    passed = decisions.get("passed_on", []) or []
    note = decisions.get("note", "")
    log.info(
        f"scan decisions: entries={len(entries)} exits={len(exits)} "
        f"passed_on={len(passed)} note={note!r}"
    )

    # Execute
    dynamic_set = _dynamic_symbols(news, earnings)
    n_entries = _execute_intents(entries, "buy", dynamic_set)
    n_exits = _execute_intents(exits, "sell", dynamic_set)

    return ScanResult(
        status="ok",
        entries=n_entries,
        exits=n_exits,
        passed_on=len(passed),
        note=note or "",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="override AUTO_TRADE_DRY_RUN=true for this run")
    parser.add_argument("--force", action="store_true",
                        help="skip market-hours guard (for testing)")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["AUTO_TRADE_DRY_RUN"] = "true"

    result = asyncio.run(run_scan(force=args.force))
    log.info(f"done: {result}")
    return 0 if result.status in ("ok",) or result.status.startswith("skipped:") else 1


if __name__ == "__main__":
    sys.exit(main())
