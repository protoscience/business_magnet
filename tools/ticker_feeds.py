"""Dynamic ticker discovery for the Phase 2 scan.

Two functions:
  - fetch_news_headlines(): returns raw news snippets from SearXNG
  - fetch_earnings_snippets(): returns raw earnings-calendar snippets

Intentionally NOT pre-extracting tickers here — we hand raw text to
Claude in the scan prompt and let the model identify which symbols are
worth considering. Lower noise than a regex + manual cleanup.

If we later add Finnhub (earnings calendar endpoint, free tier but
requires API key signup), it replaces fetch_earnings_snippets.
"""
import asyncio
import logging
from typing import TypedDict

from tools import search as search_tool

log = logging.getLogger("ticker_feeds")


class Snippet(TypedDict):
    title: str
    content: str
    source: str


_NEWS_QUERIES = [
    "biggest US stock movers today",
    "most active US stocks today premarket",
    "stocks news today",
    "earnings surprise today US market",
]

_EARNINGS_QUERIES = [
    "companies reporting earnings this week",
    "earnings calendar this week S&P",
]


async def _search_batch(queries: list[str], per_query: int) -> list[Snippet]:
    out: list[Snippet] = []
    for q in queries:
        try:
            results = await search_tool.search_web(q, max_results=per_query)
            for r in results:
                if isinstance(r, dict):
                    out.append({
                        "title": r.get("title", "")[:200],
                        "content": (r.get("content") or "")[:400],
                        "source": q,
                    })
        except Exception:
            log.exception(f"SearXNG query failed: {q!r}")
    return out


async def fetch_news_headlines(max_snippets: int = 15) -> list[Snippet]:
    """Return raw news snippets covering market movers + headlines."""
    raw = await _search_batch(_NEWS_QUERIES, per_query=5)
    # de-dup by title prefix to keep prompt signal-to-noise up
    seen: set[str] = set()
    out: list[Snippet] = []
    for s in raw:
        key = s["title"][:80].lower()
        if key and key not in seen:
            seen.add(key)
            out.append(s)
        if len(out) >= max_snippets:
            break
    return out


async def fetch_earnings_snippets(max_snippets: int = 10) -> list[Snippet]:
    """Return raw earnings-calendar snippets for this week.

    Intentionally vague — the LLM will extract tickers + dates from the
    free-form text. Swap for a proper Finnhub/IEX calendar if accuracy
    becomes a problem.
    """
    raw = await _search_batch(_EARNINGS_QUERIES, per_query=5)
    seen: set[str] = set()
    out: list[Snippet] = []
    for s in raw:
        key = s["title"][:80].lower()
        if key and key not in seen:
            seen.add(key)
            out.append(s)
        if len(out) >= max_snippets:
            break
    return out


async def fetch_all() -> tuple[list[Snippet], list[Snippet]]:
    """Convenience: fetch news + earnings in parallel."""
    news, earnings = await asyncio.gather(
        fetch_news_headlines(),
        fetch_earnings_snippets(),
    )
    return news, earnings


if __name__ == "__main__":
    async def main():
        news, earnings = await fetch_all()
        print(f"=== news ({len(news)}) ===")
        for n in news[:5]:
            print(f"  - {n['title']}")
        print(f"\n=== earnings ({len(earnings)}) ===")
        for e in earnings[:5]:
            print(f"  - {e['title']}")

    asyncio.run(main())
