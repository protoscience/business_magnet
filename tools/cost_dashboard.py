"""Render a gamified cost/token dashboard PNG from logs/cost.db.

Usage:
    python -m tools.cost_dashboard [--window all|30d|7d] [--out PATH]
"""
import argparse
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

from tools.cost_log import connect

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
OUT_DIR = Path(os.environ.get("IMAGE_OUTPUT_DIR", "/tmp/trading-agent-images"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

HEATMAP_WEEKS = 26
MOBY_DICK_TOKENS = 260_000  # ~ Moby-Dick ≈ 260k tokens (rule-of-thumb for GPT-ish tokenizers)

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _window_start(window: str, now: datetime) -> datetime | None:
    if window == "7d":
        return now - timedelta(days=7)
    if window == "30d":
        return now - timedelta(days=30)
    return None  # all


def _fetch_daily(conn, start: datetime | None) -> list[tuple]:
    """Union of raw turns (today) + rolled-up daily rows, aggregated per (date, channel, peer)."""
    params: list = []
    raw_where = ""
    daily_where = ""
    if start is not None:
        raw_where = "WHERE ts >= ?"
        daily_where = "WHERE date >= ?"
        params = [start.isoformat(), start.date().isoformat()]
    rows = conn.execute(
        f"""
        SELECT date, channel, peer, SUM(turns) AS turns, SUM(cost_usd) AS cost_usd,
               SUM(input_tokens) AS it, SUM(output_tokens) AS ot,
               SUM(cache_read_tokens) AS crt, SUM(cache_creation_tokens) AS cct
        FROM (
            SELECT date(ts) AS date, channel, peer, turns, cost_usd,
                   input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens
            FROM turns {raw_where}
            UNION ALL
            SELECT date, channel, peer, turns, cost_usd,
                   input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens
            FROM daily {daily_where}
        )
        GROUP BY date, channel, peer
        """,
        params,
    ).fetchall()
    return rows


def _fetch_hour_histogram(conn, start: datetime | None) -> dict[int, int]:
    """Turns by UTC hour-of-day from raw rows only (daily rollup loses hour info)."""
    params: list = []
    where = ""
    if start is not None:
        where = "WHERE ts >= ?"
        params = [start.isoformat()]
    rows = conn.execute(
        f"SELECT CAST(strftime('%H', ts) AS INTEGER), SUM(turns) FROM turns {where} GROUP BY 1",
        params,
    ).fetchall()
    return {h: t for h, t in rows}


def _streaks(active_dates: set) -> tuple[int, int]:
    if not active_dates:
        return 0, 0
    today = datetime.now(timezone.utc).date()
    # current streak (counts back from today or yesterday if today empty)
    cur = 0
    d = today
    if d not in active_dates:
        d = today - timedelta(days=1)
    while d in active_dates:
        cur += 1
        d -= timedelta(days=1)
    # longest streak across all active dates
    longest = 0
    sorted_dates = sorted(active_dates)
    run = 1
    for i in range(1, len(sorted_dates)):
        if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
            run += 1
        else:
            longest = max(longest, run)
            run = 1
    longest = max(longest, run)
    return cur, longest


def _heatmap_columns(daily_rows: list[tuple], weeks: int) -> list[list[int]]:
    """Return list of columns (oldest week first), each a list of 7 intensity levels (Mon..Sun)."""
    today = datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.weekday())
    # Aggregate turns per date
    per_date: dict = {}
    for row in daily_rows:
        date_str = row[0]
        turns = row[3] or 0
        d = datetime.fromisoformat(date_str).date()
        per_date[d] = per_date.get(d, 0) + turns

    max_turns = max(per_date.values(), default=0)

    def level(turns: int) -> int:
        if turns <= 0 or max_turns == 0:
            return 0
        ratio = turns / max_turns
        if ratio > 0.75:
            return 4
        if ratio > 0.5:
            return 3
        if ratio > 0.25:
            return 2
        return 1

    cols: list[list[int]] = []
    start_monday = this_monday - timedelta(weeks=weeks - 1)
    for w in range(weeks):
        week_mon = start_monday + timedelta(weeks=w)
        col = []
        for d_idx in range(7):
            d = week_mon + timedelta(days=d_idx)
            if d > today:
                col.append(0)
            else:
                col.append(level(per_date.get(d, 0)))
        cols.append(col)
    return cols


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def _build_view(window: str) -> dict:
    now = datetime.now(timezone.utc)
    start = _window_start(window, now)
    conn = connect()
    try:
        daily = _fetch_daily(conn, start)
        hour_hist = _fetch_hour_histogram(conn, start)
    finally:
        conn.close()

    # totals
    messages = sum((r[3] or 0) for r in daily)
    cost = sum((r[4] or 0.0) for r in daily)
    input_tokens = sum((r[5] or 0) for r in daily)
    output_tokens = sum((r[6] or 0) for r in daily)
    cache_read = sum((r[7] or 0) for r in daily)
    total_tokens = input_tokens + output_tokens + cache_read

    # sessions = unique (peer, date) pairs
    sessions_pairs = {(r[0], r[1], r[2]) for r in daily if (r[3] or 0) > 0}
    sessions = len(sessions_pairs)

    # active days
    active_dates = {datetime.fromisoformat(r[0]).date() for r in daily if (r[3] or 0) > 0}
    active_days = len(active_dates)

    cur_streak, longest = _streaks(active_dates)

    # peak hour (UTC). Format in local-ish 12h style like "7 PM".
    if hour_hist:
        peak_h = max(hour_hist, key=lambda h: hour_hist[h])
        # Convert UTC hour to America/New_York approx (ET is UTC-4 or -5; we use -4 DST-ish)
        et_h = (peak_h - 4) % 24
        suffix = "AM" if et_h < 12 else "PM"
        display_h = et_h % 12 or 12
        peak_hour = f"{display_h} {suffix}"
    else:
        peak_hour = "—"

    # top peer
    per_peer: dict = {}
    for r in daily:
        key = (r[1], r[2])
        per_peer[key] = per_peer.get(key, 0.0) + (r[4] or 0.0)
    if per_peer:
        (ch, peer), _ = max(per_peer.items(), key=lambda kv: kv[1])
        top_peer = f"{ch}:{peer[:12]}"
    else:
        top_peer = "—"

    # footer
    if total_tokens > 0:
        mult = total_tokens / MOBY_DICK_TOKENS
        if mult >= 1:
            footer = f"You've used ~{mult:.1f}× more tokens than Moby-Dick."
        else:
            footer = f"You've used ~{mult*100:.0f}% of a Moby-Dick in tokens so far."
    else:
        footer = "No token data yet — start chatting with Sonic or SuperSonic."

    return {
        "window": window,
        "weeks": HEATMAP_WEEKS,
        "sessions": sessions,
        "messages": messages,
        "total_tokens_display": _format_tokens(total_tokens),
        "active_days": active_days,
        "current_streak": cur_streak,
        "longest_streak": longest,
        "peak_hour": peak_hour,
        "top_peer": top_peer,
        "heatmap": _heatmap_columns(daily, HEATMAP_WEEKS),
        "footer": footer,
        "_cost": cost,
    }


async def render_dashboard(window: str = "all", out_path: str | None = None) -> str:
    view = _build_view(window)
    html = _env.get_template("cost_dashboard.html").render(**view)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = await browser.new_context(
                viewport={"width": 1000, "height": 100},
                device_scale_factor=2,
            )
            page = await context.new_page()
            await page.set_content(html, wait_until="networkidle")
            path = out_path or str(OUT_DIR / f"cost_dashboard_{uuid.uuid4().hex[:8]}.png")
            await page.screenshot(path=path, full_page=True, type="png")
            await context.close()
        finally:
            await browser.close()

    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", choices=["all", "30d", "7d"], default="all")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    path = asyncio.run(render_dashboard(args.window, args.out))
    print(path)


if __name__ == "__main__":
    main()
