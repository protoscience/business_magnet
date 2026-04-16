import asyncio
import os
import uuid
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright


OUT_DIR = os.environ.get("IMAGE_OUTPUT_DIR", "/tmp/trading-agent-images")
os.makedirs(OUT_DIR, exist_ok=True)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

_playwright_ctx = None
_browser = None
_browser_lock = asyncio.Lock()


async def _get_browser():
    global _playwright_ctx, _browser
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            _playwright_ctx = await async_playwright().start()
            _browser = await _playwright_ctx.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
    return _browser


VERDICT_MAP = {
    "BULLISH": ("bull", "📈"),
    "BUY": ("bull", "🟢"),
    "BEARISH": ("bear", "📉"),
    "SELL": ("bear", "🔴"),
    "NEUTRAL": ("neutral", "➖"),
    "HOLD": ("neutral", "⏸️"),
    "WATCH": ("watch", "👀"),
    "CAUTION": ("watch", "⚠️"),
}


async def render_analysis_image(
    symbol: str,
    name: str | None = None,
    price: float | None = None,
    change_pct: float | None = None,
    verdict: str | None = None,
    headline: str | None = None,
    metrics: list[dict] | None = None,
    sections: list[dict] | None = None,
    warnings: list[str] | None = None,
) -> str:
    kind, icon = (None, None)
    if verdict:
        kind, icon = VERDICT_MAP.get(verdict.upper(), ("neutral", ""))

    html = _env.get_template("analysis_card.html").render(
        symbol=symbol.upper(),
        name=name,
        price=price,
        change_pct=change_pct,
        verdict=verdict.upper() if verdict else None,
        verdict_kind=kind,
        verdict_icon=icon,
        headline=headline,
        metrics=metrics or [],
        sections=sections or [],
        warnings=warnings or [],
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    browser = await _get_browser()
    context = await browser.new_context(
        viewport={"width": 960, "height": 100},
        device_scale_factor=2,
    )
    page = await context.new_page()
    try:
        await page.set_content(html, wait_until="networkidle")
        path = os.path.join(OUT_DIR, f"analysis_{symbol.lower()}_{uuid.uuid4().hex[:8]}.png")
        await page.screenshot(path=path, full_page=True, type="png")
    finally:
        await context.close()

    return path


async def shutdown():
    global _browser, _playwright_ctx
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright_ctx:
        await _playwright_ctx.stop()
        _playwright_ctx = None
