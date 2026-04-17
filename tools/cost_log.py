import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "logs" / "cost.db"

_TOKEN_COLS = "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    ts TEXT NOT NULL,
    channel TEXT NOT NULL,
    peer TEXT NOT NULL,
    turns INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);

CREATE TABLE IF NOT EXISTS daily (
    date TEXT NOT NULL,
    channel TEXT NOT NULL,
    peer TEXT NOT NULL,
    turns INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, channel, peer)
);

CREATE TABLE IF NOT EXISTS weekly (
    week_start TEXT NOT NULL,
    channel TEXT NOT NULL,
    peer TEXT NOT NULL,
    turns INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (week_start, channel, peer)
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _extract_tokens(usage: dict | None) -> tuple[int, int, int, int]:
    u = usage or {}
    return (
        int(u.get("input_tokens", 0) or 0),
        int(u.get("output_tokens", 0) or 0),
        int(u.get("cache_read_input_tokens", 0) or 0),
        int(u.get("cache_creation_input_tokens", 0) or 0),
    )


def log_turn(
    channel: str,
    peer: str,
    turns: int,
    cost_usd: float,
    usage: dict | None = None,
) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    i_tok, o_tok, cr_tok, cc_tok = _extract_tokens(usage)
    conn = connect()
    try:
        conn.execute(
            f"INSERT INTO turns (ts, channel, peer, turns, cost_usd, {_TOKEN_COLS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, channel, str(peer), int(turns or 0), float(cost_usd or 0.0),
             i_tok, o_tok, cr_tok, cc_tok),
        )
        conn.commit()
    finally:
        conn.close()
