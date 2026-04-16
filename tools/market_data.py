import os
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame


_client: StockHistoricalDataClient | None = None


def _get_client() -> StockHistoricalDataClient:
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
        )
    return _client


def get_latest_quote(symbol: str) -> dict:
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol.upper())
    quotes = _get_client().get_stock_latest_quote(req)
    q = quotes[symbol.upper()]
    return {
        "symbol": symbol.upper(),
        "bid_price": float(q.bid_price),
        "ask_price": float(q.ask_price),
        "bid_size": int(q.bid_size),
        "ask_size": int(q.ask_size),
        "timestamp": q.timestamp.isoformat(),
    }


def get_recent_bars(symbol: str, days: int = 30, timeframe: str = "1Day") -> list[dict]:
    tf_map = {
        "1Min": TimeFrame.Minute,
        "5Min": TimeFrame(5, TimeFrame.Minute.unit),
        "15Min": TimeFrame(15, TimeFrame.Minute.unit),
        "1Hour": TimeFrame.Hour,
        "1Day": TimeFrame.Day,
    }
    tf = tf_map.get(timeframe, TimeFrame.Day)

    req = StockBarsRequest(
        symbol_or_symbols=symbol.upper(),
        timeframe=tf,
        start=datetime.now(timezone.utc) - timedelta(days=days),
    )
    bars = _get_client().get_stock_bars(req)
    out = []
    for b in bars[symbol.upper()]:
        out.append({
            "t": b.timestamp.strftime("%m/%d"),
            "o": round(float(b.open), 2),
            "h": round(float(b.high), 2),
            "l": round(float(b.low), 2),
            "c": round(float(b.close), 2),
            "v": int(b.volume),
        })
    return out
