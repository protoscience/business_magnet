"""Nightly rollup: raw turns -> daily, daily -> weekly, then prune.

Raw turns older than today (UTC) are summed into `daily` and deleted.
`weekly` is rebuilt from `daily` each run (cheap at our scale).
`daily` rows older than DAILY_RETENTION_DAYS are dropped.
"""
from datetime import datetime, timedelta, timezone

from tools.cost_log import connect

DAILY_RETENTION_DAYS = 365

_SUM_COLS = ("turns", "cost_usd", "input_tokens", "output_tokens",
             "cache_read_tokens", "cache_creation_tokens")


def _rollup_raw_to_daily(conn) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    sums_select = ", ".join(f"SUM({c})" for c in _SUM_COLS)
    cols = ", ".join(_SUM_COLS)
    updates = ", ".join(f"{c} = daily.{c} + excluded.{c}" for c in _SUM_COLS)
    cur = conn.execute(
        f"""
        INSERT INTO daily (date, channel, peer, {cols})
        SELECT date(ts), channel, peer, {sums_select}
        FROM turns
        WHERE date(ts) < ?
        GROUP BY date(ts), channel, peer
        ON CONFLICT(date, channel, peer) DO UPDATE SET {updates}
        """,
        (today,),
    )
    rolled = cur.rowcount
    conn.execute("DELETE FROM turns WHERE date(ts) < ?", (today,))
    return rolled


def _rebuild_weekly(conn) -> None:
    conn.execute("DELETE FROM weekly")
    sums_select = ", ".join(f"SUM({c})" for c in _SUM_COLS)
    cols = ", ".join(_SUM_COLS)
    # strftime('%w'): Sunday=0..Saturday=6. Monday-based week_start:
    #   offset_days = (w + 6) % 7  (Mon→0, Tue→1, ..., Sun→6)
    conn.execute(
        f"""
        INSERT INTO weekly (week_start, channel, peer, {cols})
        SELECT
            date(date, '-' || ((CAST(strftime('%w', date) AS INTEGER) + 6) % 7) || ' days') AS week_start,
            channel, peer, {sums_select}
        FROM daily
        GROUP BY week_start, channel, peer
        """
    )


def _prune_daily(conn) -> int:
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=DAILY_RETENTION_DAYS)).isoformat()
    cur = conn.execute("DELETE FROM daily WHERE date < ?", (cutoff,))
    return cur.rowcount


def main() -> None:
    conn = connect()
    try:
        rolled = _rollup_raw_to_daily(conn)
        _rebuild_weekly(conn)
        pruned = _prune_daily(conn)
        conn.commit()
        print(f"cost-rollup: raw→daily groups={rolled}, weekly rebuilt, daily pruned={pruned}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
