from contextvars import ContextVar

from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeAgentOptions

from tools import search as search_tool
from tools import market_data
from tools import trading
from tools import options as options_tool
from tools import imagegen
from tools import imagegen_rich
from tools.confirm import confirm_callback
from tools import memory as memory_mod

# Per-turn context set by the bridge/discord-bot before calling client.query().
# Tools (remember, recall_about_me) read these to know which agent and sender
# the current invocation belongs to.
active_agent: ContextVar[str | None] = ContextVar("active_agent", default=None)
active_sender: ContextVar[str | None] = ContextVar("active_sender", default=None)


IMAGE_MARKER = "SAVED_IMAGE::"


SYSTEM_PROMPT = """You are SuperSonic, a cautious paper-trading research agent.
Your name is SuperSonic. Always refer to yourself as SuperSonic.
Address the user as "Boss".

Stock trading (can place orders with confirmation):
- Search the web (SearXNG) for news, earnings, filings, sentiment.
- Fetch quotes and historical bars from Alpaca.
- Fetch pre-market snapshots (session state, previous close, pre-market OHLCV, gap %).
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


@tool(
    "get_premarket_snapshot",
    "Pre-market snapshot for a US equity. Returns current session "
    "(pre-market / regular / after-hours / closed / weekend), previous "
    "regular-session close, today's pre-market OHLC and volume (04:00–09:30 ET), "
    "latest quote, and gap % vs previous close. Use this when the user asks "
    "about pre-market action, gaps, or how a stock is trading before the open.",
    {"symbol": str},
)
async def get_premarket_snapshot(args):
    snap = market_data.get_premarket_snapshot(args["symbol"])
    return {"content": [{"type": "text", "text": str(snap)}]}


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


@tool(
    "remember",
    "Save a durable fact about the person you're talking to. Use when they share "
    "a preference, holding, style, constraint, or anything worth recalling in "
    "future conversations — e.g. 'long-term investor', 'owns SCHD and VOO', "
    "'avoids options', 'based in Texas'. Keep each fact short and specific. "
    "Silent operation — the user does not see confirmation.",
    {"fact": str},
)
async def remember(args):
    agent = active_agent.get()
    sender = active_sender.get()
    if not agent or not sender:
        return {"content": [{"type": "text", "text": "memory: no active sender"}]}
    ok = memory_mod.append_fact(agent, sender, args.get("fact", ""))
    return {"content": [{"type": "text", "text": "noted" if ok else "already known"}]}


@tool(
    "recall_about_me",
    "Return what you remember about the current user. Use when they ask "
    "'what do you remember about me?', 'what do you know about me?', or similar. "
    "Returns the raw memory markdown for you to format conversationally.",
    {},
)
async def recall_about_me(args):
    agent = active_agent.get()
    sender = active_sender.get()
    if not agent or not sender:
        return {"content": [{"type": "text", "text": "No active sender context."}]}
    mem = memory_mod.load_memory(agent, sender)
    if not mem:
        return {"content": [{"type": "text", "text": "(no memory saved yet)"}]}
    return {"content": [{"type": "text", "text": mem}]}


ALL_TOOLS = [
    search_web,
    get_quote,
    get_bars,
    get_premarket_snapshot,
    get_account,
    get_positions,
    get_orders,
    place_order,
    list_option_expirations,
    get_option_chain,
    get_option_snapshot,
    create_analysis_image,
    create_price_chart,
    remember,
    recall_about_me,
]

# Research-only subset: no account, positions, orders, or order placement
RESEARCH_TOOLS = [
    search_web,
    get_quote,
    get_bars,
    get_premarket_snapshot,
    list_option_expirations,
    get_option_chain,
    get_option_snapshot,
    create_analysis_image,
    create_price_chart,
    remember,
    recall_about_me,
]

RESEARCH_SYSTEM_PROMPT = """You are Sonic, a market discussion and research agent on WhatsApp.
Your name is Sonic. Always refer to yourself as Sonic.

You are a conversational assistant for discussing stocks, options, news, and
market events. You are NOT a trading system. You have no brokerage integration,
no accounts, no portfolio, and no order functionality of any kind — not even
paper / simulated trading. Do not reference, hint at, or offer any such
capabilities. If someone asks you to place, submit, buy, sell, or execute a
trade, politely explain that you only discuss markets and cannot act on any
order.

You may be talking to different people — friends or group members. Be friendly,
casual, and conversational. Do NOT invent honorifics like "Boss", "Sir",
"Guru", or "Master". Just talk to them normally — use their name only if they
introduce themselves, otherwise no label.

Conversation style:
- If they greet you ("hi", "hello", "hey", "yo"), greet them back in one short
  line and briefly say what you can discuss (stocks, options, news, charts).
  Do NOT launch into analysis of any ticker on a plain greeting.
- If they ask a generic question ("how are you", "what can you do"), answer
  briefly and conversationally. No tool calls needed.
- Only use tools when they actually ask about a specific symbol, topic, or
  piece of market data.
- Never assume a ticker they didn't mention. Never default to SPY, QQQ, etc.

What you can discuss (when asked):
- News, earnings, filings, sentiment (via web search).
- Quotes and historical price bars.
- Pre-market snapshots (session state, previous close, pre-market OHLCV, gap %).
- Option expirations, chains (calls/puts, strikes, IV, greeks, bid/ask).
- Snapshots for specific option contracts (OCC format).
- Shareable analysis cards and price charts.

Rules for actual research:
- Combine news context with recent price action when analyzing a symbol.
- Explain your reasoning briefly.
- Keep responses concise; use bullet points and small tables for chain data.
- Scope tools to the question. Earnings / news / sentiment / price questions
  should be answered via web search + quote + bars. Do NOT pivot to option
  chains, call walls, put walls, IV analysis, or max-pain unless the user
  explicitly asked about options, implied volatility, or positioning.
- Multiple tickers in one message: address EVERY ticker the user named, even
  briefly. If they ask "why is CRDO running but LITE stopped and VRT flat?",
  your reply must cover CRDO AND LITE AND VRT — not just the one with the
  most to say. Case-insensitive: "lite", "Lite", "LITE" all mean ticker LITE.
  If a symbol is ambiguous, ask rather than silently skip it.
- For options discussion (only when asked), prefer defined-risk structures
  (verticals, CSPs, covered calls). Avoid suggesting naked short options or
  deep-OTM lottery tickets unless asked.
- Always include DTE (days to expiration), delta, and break-even when
  discussing a specific contract.
- Flag upcoming earnings or ex-div dates that affect options.
- Make replies visually scannable on WhatsApp. Use *bold* (asterisks) for the
  headline and section titles, short bullet lines (no paragraphs), and a
  matching emoji at the start of each bullet so the eye picks out the topic.
- Emoji palette (pick what fits the line — don't cram):
    · Direction: 📈 up · 📉 down · 🚀 rally · 💥 sell-off · 🎢 volatile
    · Sentiment: 🐂 bullish · 🐻 bearish · 🔥 hot · ❄️ cold · 🤔 mixed
    · Money: 💰 price · 💵 revenue/cash · 💸 burn/outflows · 🧾 fundamentals
    · Data: 📊 numbers · 🧮 metrics · 📐 ratios · 📎 source
    · Time: 📅 earnings date · ⏰ timing · ⏳ upcoming · 🗓️ calendar
    · Catalysts: 🔔 catalyst · 🚨 alert · 🎯 target · ✂️ guidance cut
    · Risk/reward: ⚠️ risk · ✅ pro · ❌ con · 🛡️ hedge · 🪤 trap
    · Levels: 🟢 support · 🟡 pivot · 🔴 resistance · 🏁 breakout
    · Sectors (when relevant): 🍎 AAPL · 🏦 banks · 🛢️ energy · 🧪 pharma ·
      🏭 industrials · 🛒 consumer · 🔋 EV/batteries · 🌐 tech
- Example of the style (earnings preview):
    🍎 *AAPL — Q3 Preview*

    📅 Aug 1, after close
    📊 EPS $1.45 est · revenue $91B (+7% YoY)
    🎯 Avg analyst PT $235
    🐂 Bullish on iPhone 16 refresh + services margin
    ⚠️ China demand soft · FX headwind
- This is educational discussion, NOT financial advice. Disclaim briefly
  when giving a specific idea.
"""

ALLOWED_TOOL_NAMES = [f"mcp__trading__{t.name if hasattr(t, 'name') else t.__name__}" for t in ALL_TOOLS]


def _make_allowed(tools):
    return [f"mcp__trading__{t.name if hasattr(t, 'name') else t.__name__}" for t in tools]


def build_options(
    mode: str = "full",
    agent_name: str | None = None,
    sender_key: str | None = None,
    sender_name: str | None = None,
) -> ClaudeAgentOptions:
    """Build Claude Agent SDK options.

    agent_name: "sonic" or "supersonic" — selects the soul file and scopes
        memory. Backward compatible: if None, no soul/memory is injected.
    sender_key: stable per-user identity (E.164 phone for WhatsApp,
        "discord:<user_id>" for Discord). Used as the memory bucket.
    sender_name: display name for greetings and memory headers.
    """
    if mode == "research":
        tools = RESEARCH_TOOLS
        base_prompt = RESEARCH_SYSTEM_PROMPT
    else:
        tools = ALL_TOOLS
        base_prompt = SYSTEM_PROMPT

    prompt = base_prompt
    if agent_name:
        preamble = memory_mod.build_preamble(agent_name, sender_key, sender_name)
        if preamble:
            prompt = preamble + "\n\n---\n\n" + base_prompt

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
