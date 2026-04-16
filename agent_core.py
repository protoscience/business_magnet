from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeAgentOptions

from tools import search as search_tool
from tools import market_data
from tools import trading
from tools import options as options_tool
from tools import imagegen
from tools import imagegen_rich
from tools.confirm import confirm_callback


IMAGE_MARKER = "SAVED_IMAGE::"


SYSTEM_PROMPT = """You are a cautious paper-trading research agent.

Stock trading (can place orders with confirmation):
- Search the web (SearXNG) for news, earnings, filings, sentiment.
- Fetch quotes and historical bars from Alpaca.
- Inspect paper account, positions, and orders.
- Place paper stock orders (buy/sell, market/limit). All orders require human confirmation.

Options (RESEARCH ONLY — never place option orders, only suggest ideas):
- List available expirations for an underlying.
- Fetch option chain (calls/puts, strikes, IV, greeks, bid/ask) for a given expiration.
- Fetch snapshot for a specific option contract symbol (OCC format).
- When asked for option ideas, suggest concrete contracts with rationale, expected risk/reward,
  and key greeks — but DO NOT attempt to place option orders.

Rules:
- Paper account only. Never claim this is real money.
- Before placing stock orders, check buying power and current positions.
- Size conservatively unless the user specifies otherwise.
- Combine news context with recent price action when analyzing a symbol.
- Explain your reasoning briefly before proposing trades.
- Keep responses concise; use bullet points and small tables for chain data.
- For any analysis, research summary, trade idea, options idea, or when the user
  asks for something shareable, DEFAULT to producing a create_analysis_image.
  BE TERSE. The card is read at a glance, not like a document.
    * headline: max ~8 words
    * metrics: 4 items (never more)
    * sections: 2-3 sections max, each with 2-3 bullets
    * bullets: ≤ 7 words each. No full sentences. Fragments only.
      GOOD: "EPS beat by 12%", "Guidance raised Q3"
      BAD:  "The company reported earnings per share that beat consensus estimates by 12%"
    * warnings: 1-2 short phrases
  Keep accompanying chat text to 1 line — the image carries the content.
- Use create_price_chart for pure price action / technical views with annotation levels.
- Pick emoji icons that match meaning: 📈📉 for direction, 💰 price/cash,
  📅 dates/earnings, ⚠️ risk, ✅❌ pros/cons, 🎯 targets, 🔔 catalysts,
  ⏰ timing, 📊 data, 🧮 numbers, 🟢🔴🟡 levels.
- For options ideas, default to defined-risk structures (verticals, CSPs, covered calls).
  Avoid suggesting naked short options or deep-OTM lottery tickets unless the user asks.
- Always include DTE (days to expiration), delta, and break-even when recommending a contract.
- Flag upcoming earnings or ex-div dates that affect option positions.
"""


@tool(
    "search_web",
    "Search the web via SearXNG. Returns recent articles, news, and pages.",
    {"query": str, "max_results": int},
)
async def search_web(args):
    results = await search_tool.search_web(
        args["query"], max_results=args.get("max_results", 5)
    )
    return {"content": [{"type": "text", "text": str(results)}]}


@tool(
    "get_quote",
    "Get the latest bid/ask quote for a stock symbol.",
    {"symbol": str},
)
async def get_quote(args):
    q = market_data.get_latest_quote(args["symbol"])
    return {"content": [{"type": "text", "text": str(q)}]}


@tool(
    "get_bars",
    "Get recent OHLCV bars for a symbol. timeframe: 1Min, 5Min, 15Min, 1Hour, 1Day.",
    {"symbol": str, "days": int, "timeframe": str},
)
async def get_bars(args):
    bars = market_data.get_recent_bars(
        args["symbol"],
        days=args.get("days", 30),
        timeframe=args.get("timeframe", "1Day"),
    )
    return {"content": [{"type": "text", "text": str(bars)}]}


@tool("get_account", "Get paper trading account status and buying power.", {})
async def get_account(args):
    return {"content": [{"type": "text", "text": str(trading.get_account())}]}


@tool("get_positions", "List all open paper trading positions.", {})
async def get_positions(args):
    return {"content": [{"type": "text", "text": str(trading.get_positions())}]}


@tool(
    "get_orders",
    "List recent orders. status: open, closed, all.",
    {"status": str, "limit": int},
)
async def get_orders(args):
    return {
        "content": [{
            "type": "text",
            "text": str(trading.get_orders(args.get("status", "open"), args.get("limit", 20))),
        }]
    }


@tool(
    "place_order",
    "Place a paper trading order. side: buy|sell. order_type: market|limit. Requires user confirmation.",
    {
        "symbol": str,
        "qty": float,
        "side": str,
        "order_type": str,
        "limit_price": float,
        "time_in_force": str,
    },
)
async def place_order(args):
    limit_price = args.get("limit_price")
    order_type = args.get("order_type", "market")
    tif = args.get("time_in_force", "day")
    price_part = f" @ {limit_price}" if limit_price else ""
    summary = (
        f"PAPER ORDER: {args['side'].upper()} {args['qty']} {args['symbol'].upper()} "
        f"[{order_type}{price_part}, {tif}]"
    )

    cb = confirm_callback.get()
    if cb is None:
        return {"content": [{"type": "text", "text": "Refused: no confirmation handler configured."}]}

    ok = await cb(summary)
    if not ok:
        return {"content": [{"type": "text", "text": f"Order cancelled by user: {summary}"}]}

    result = trading.submit_order(
        symbol=args["symbol"],
        qty=args["qty"],
        side=args["side"],
        order_type=args.get("order_type", "market"),
        limit_price=args.get("limit_price"),
        time_in_force=args.get("time_in_force", "day"),
    )
    return {"content": [{"type": "text", "text": str(result)}]}


@tool(
    "list_option_expirations",
    "List available option expiration dates for a stock symbol. Returns ISO dates (YYYY-MM-DD).",
    {"symbol": str, "limit": int},
)
async def list_option_expirations(args):
    dates = options_tool.list_expirations(
        args["symbol"], limit=args.get("limit", 12)
    )
    return {"content": [{"type": "text", "text": str(dates)}]}


@tool(
    "get_option_chain",
    "Get option chain for a symbol on a specific expiration date (YYYY-MM-DD). "
    "Filters: option_type ('call'|'put'), strike_min, strike_max. "
    "Returns each contract with bid/ask, IV, delta, gamma, theta, vega, OI.",
    {
        "symbol": str,
        "expiration": str,
        "option_type": str,
        "strike_min": float,
        "strike_max": float,
    },
)
async def get_option_chain(args):
    chain = options_tool.get_chain(
        symbol=args["symbol"],
        expiration=args["expiration"],
        option_type=args.get("option_type"),
        strike_min=args.get("strike_min"),
        strike_max=args.get("strike_max"),
    )
    return {"content": [{"type": "text", "text": str(chain)}]}


@tool(
    "get_option_snapshot",
    "Get live snapshot (bid/ask, IV, full greeks) for a specific option contract "
    "by its OCC symbol, e.g. AAPL250620C00200000.",
    {"option_symbol": str},
)
async def get_option_snapshot(args):
    snap = options_tool.get_contract_snapshot(args["option_symbol"])
    return {"content": [{"type": "text", "text": str(snap)}]}


@tool(
    "create_analysis_image",
    """Render a rich shareable analysis card PNG. Use this to present research,
option ideas, trade setups, or portfolio reviews as a visual summary.

Fields:
  symbol (required): ticker
  name: full company name
  price: current price
  change_pct: today's percent change (e.g. 1.23 or -0.87)
  verdict: one of BULLISH, BEARISH, NEUTRAL, HOLD, WATCH, CAUTION, BUY, SELL
  headline: one-line summary next to the verdict badge
  metrics: list of {label, value, kind?}  (kind: "up"|"down" colors value)
           e.g. [{"label":"Market Cap","value":"$2.5T"},{"label":"P/E","value":"29.4"}]
  sections: list of {icon, title, kind?, bullets}
            kind: "bull"|"bear"|"risk"|"" — colors the left accent bar
            bullets: list of strings OR list of {icon, text} for per-bullet icons
            e.g. [{"icon":"📈","title":"Bull Case","kind":"bull",
                   "bullets":[{"icon":"✅","text":"Beat earnings"},
                              {"icon":"💰","text":"Dividend raise"}]}]
  warnings: list of strings — shown in a red-outlined risk panel at the bottom

Keep bullets short (one line ideal). Prefer 2-4 sections with 2-5 bullets each.
Use emoji icons freely (📈 📉 💰 📅 ⚠️ ✅ ❌ 🎯 🔔 ⏰ 📊 🧮 etc.).""",
    {
        "symbol": str,
        "name": str,
        "price": float,
        "change_pct": float,
        "verdict": str,
        "headline": str,
        "metrics": list,
        "sections": list,
        "warnings": list,
    },
)
async def create_analysis_image(args):
    path = await imagegen_rich.render_analysis_image(
        symbol=args["symbol"],
        name=args.get("name"),
        price=args.get("price"),
        change_pct=args.get("change_pct"),
        verdict=args.get("verdict"),
        headline=args.get("headline"),
        metrics=args.get("metrics") or [],
        sections=args.get("sections") or [],
        warnings=args.get("warnings") or [],
    )
    return {"content": [{"type": "text", "text": f"{IMAGE_MARKER}{path}"}]}


@tool(
    "create_price_chart",
    "Render a price chart PNG (candlesticks + volume + SMA20) for a stock. "
    "Optional annotations draw horizontal lines at specific prices (e.g. entry, target, stop). "
    "Each annotation is {price: float, label: str, color: str (hex)}.",
    {
        "symbol": str,
        "days": int,
        "timeframe": str,
        "title": str,
        "annotations": list,
    },
)
async def create_price_chart(args):
    path = imagegen.render_price_chart(
        symbol=args["symbol"],
        days=args.get("days", 60),
        timeframe=args.get("timeframe", "1Day"),
        title=args.get("title"),
        annotations=args.get("annotations"),
    )
    return {"content": [{"type": "text", "text": f"{IMAGE_MARKER}{path}"}]}


ALL_TOOLS = [
    search_web,
    get_quote,
    get_bars,
    get_account,
    get_positions,
    get_orders,
    place_order,
    list_option_expirations,
    get_option_chain,
    get_option_snapshot,
    create_analysis_image,
    create_price_chart,
]

# Research-only subset: no account, positions, orders, or order placement
RESEARCH_TOOLS = [
    search_web,
    get_quote,
    get_bars,
    list_option_expirations,
    get_option_chain,
    get_option_snapshot,
    create_analysis_image,
    create_price_chart,
]

RESEARCH_SYSTEM_PROMPT = """You are a stock and options research agent.

Capabilities:
- Search the web (SearXNG) for news, earnings, filings, sentiment.
- Fetch quotes and historical bars from Alpaca.
- List option expirations, fetch option chains (calls/puts, strikes, IV, greeks, bid/ask).
- Fetch snapshot for a specific option contract (OCC format).
- Generate analysis card images and price charts.

You DO NOT have access to any trading account. You cannot place orders, view
positions, or check account balances. You are purely a research and discussion tool.

Rules:
- Combine news context with recent price action when analyzing a symbol.
- Explain your reasoning briefly.
- Keep responses concise; use bullet points and small tables for chain data.
- For options ideas, default to defined-risk structures (verticals, CSPs, covered calls).
  Avoid suggesting naked short options or deep-OTM lottery tickets unless the user asks.
- Always include DTE (days to expiration), delta, and break-even when recommending a contract.
- Flag upcoming earnings or ex-div dates that affect option positions.
- Pick emoji icons that match meaning: 📈📉 for direction, 💰 price/cash,
  📅 dates/earnings, ⚠️ risk, ✅❌ pros/cons, 🎯 targets, 🔔 catalysts.
- This is NOT financial advice. Always disclaim.
"""

ALLOWED_TOOL_NAMES = [f"mcp__trading__{t.name if hasattr(t, 'name') else t.__name__}" for t in ALL_TOOLS]


def _make_allowed(tools):
    return [f"mcp__trading__{t.name if hasattr(t, 'name') else t.__name__}" for t in tools]


def build_options(mode: str = "full") -> ClaudeAgentOptions:
    if mode == "research":
        tools = RESEARCH_TOOLS
        prompt = RESEARCH_SYSTEM_PROMPT
    else:
        tools = ALL_TOOLS
        prompt = SYSTEM_PROMPT

    server = create_sdk_mcp_server(
        name="trading-tools",
        version="1.0.0",
        tools=tools,
    )
    return ClaudeAgentOptions(
        system_prompt=prompt,
        mcp_servers={"trading": server},
        allowed_tools=_make_allowed(tools),
        permission_mode="acceptEdits",
    )
