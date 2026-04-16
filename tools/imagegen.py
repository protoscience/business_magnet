import os
import uuid
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from tools import market_data


OUT_DIR = os.environ.get("IMAGE_OUTPUT_DIR", "/tmp/trading-agent-images")
os.makedirs(OUT_DIR, exist_ok=True)


def _new_path(prefix: str) -> str:
    return os.path.join(OUT_DIR, f"{prefix}_{uuid.uuid4().hex[:8]}.png")


def render_research_card(
    title: str,
    subtitle: str | None = None,
    bullets: list[str] | None = None,
    footer: str | None = None,
) -> str:
    bullets = bullets or []
    n_bullets = len(bullets)
    fig_h = max(4.5, 1.6 + 0.55 * n_bullets + (0.6 if footer else 0))
    fig, ax = plt.subplots(figsize=(8.5, fig_h))
    ax.set_axis_off()
    ax.add_patch(Rectangle((0, 0), 1, 1, transform=ax.transAxes, color="#0f172a", zorder=-2))
    ax.add_patch(Rectangle((0, 0.88), 1, 0.12, transform=ax.transAxes, color="#1e293b", zorder=-1))

    fig.text(0.04, 0.93, title, fontsize=18, fontweight="bold", color="#e2e8f0", va="center")
    if subtitle:
        fig.text(0.04, 0.89, subtitle, fontsize=11, color="#94a3b8", va="center")

    y = 0.80
    for b in bullets:
        fig.text(0.06, y, "•", fontsize=14, color="#38bdf8", va="top")
        fig.text(0.09, y, b, fontsize=11, color="#e2e8f0", va="top", wrap=True)
        y -= 0.055 + 0.01 * max(0, (len(b) // 90))

    if footer:
        fig.text(0.04, 0.04, footer, fontsize=9, color="#64748b", va="bottom")

    fig.text(
        0.96, 0.04,
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        fontsize=8, color="#475569", va="bottom", ha="right",
    )

    path = _new_path("card")
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor="#0f172a")
    plt.close(fig)
    return path


def render_price_chart(
    symbol: str,
    days: int = 60,
    timeframe: str = "1Day",
    title: str | None = None,
    annotations: list[dict] | None = None,
) -> str:
    bars = market_data.get_recent_bars(symbol, days=days, timeframe=timeframe)
    if not bars:
        raise ValueError(f"No bars returned for {symbol}")

    ts = [datetime.fromisoformat(b["t"].replace("Z", "+00:00")) for b in bars]
    opens = [b["o"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]

    fig, (ax_p, ax_v) = plt.subplots(
        2, 1, figsize=(10, 6),
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.05},
        sharex=True,
    )
    fig.patch.set_facecolor("#0f172a")

    for ax in (ax_p, ax_v):
        ax.set_facecolor("#0f172a")
        ax.tick_params(colors="#94a3b8")
        for spine in ax.spines.values():
            spine.set_color("#334155")
        ax.grid(True, color="#1e293b", linewidth=0.5)

    width = (ts[1] - ts[0]).total_seconds() / 86400 * 0.7 if len(ts) > 1 else 0.7
    for t, o, h, l, c in zip(ts, opens, highs, lows, closes):
        color = "#22c55e" if c >= o else "#ef4444"
        ax_p.vlines(t, l, h, color=color, linewidth=0.8)
        ax_p.add_patch(Rectangle(
            (matplotlib.dates.date2num(t) - width / 2, min(o, c)),
            width, max(abs(c - o), 0.01),
            color=color, zorder=3,
        ))

    if len(closes) >= 20:
        sma20 = [sum(closes[max(0, i-19):i+1]) / min(20, i+1) for i in range(len(closes))]
        ax_p.plot(ts, sma20, color="#f59e0b", linewidth=1.2, label="SMA20", alpha=0.9)

    last = closes[-1]
    chart_title = title or f"{symbol.upper()} — ${last:.2f}  ({timeframe}, {days}d)"
    ax_p.set_title(chart_title, color="#e2e8f0", fontsize=13, fontweight="bold", loc="left", pad=10)

    if annotations:
        for a in annotations:
            price = a.get("price")
            label = a.get("label", "")
            color = a.get("color", "#38bdf8")
            if price is not None:
                ax_p.axhline(price, color=color, linestyle="--", linewidth=1, alpha=0.8)
                ax_p.text(ts[-1], price, f"  {label} ${price:.2f}",
                          color=color, fontsize=9, va="center")

    vol_colors = ["#22c55e" if c >= o else "#ef4444" for o, c in zip(opens, closes)]
    ax_v.bar(ts, volumes, width=width, color=vol_colors, alpha=0.6)
    ax_v.set_ylabel("Vol", color="#94a3b8", fontsize=9)

    ax_p.legend(loc="upper left", facecolor="#0f172a", edgecolor="#334155",
                labelcolor="#e2e8f0", fontsize=9)
    fig.autofmt_xdate()

    path = _new_path(f"chart_{symbol.lower()}")
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor="#0f172a")
    plt.close(fig)
    return path
