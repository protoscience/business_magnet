import os
import httpx


SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8080")


async def search_web(query: str, max_results: int = 8) -> list[dict]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json", "safesearch": 0},
            headers={"User-Agent": "trading-agent/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for r in data.get("results", [])[:max_results]:
        snippet = r.get("content", "")[:200]
        results.append({
            "title": r.get("title", ""),
            "snippet": snippet,
        })
    return results
