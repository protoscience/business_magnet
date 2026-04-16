import os
from datetime import date

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest


_trading: TradingClient | None = None
_data: OptionHistoricalDataClient | None = None


def _t() -> TradingClient:
    global _trading
    if _trading is None:
        _trading = TradingClient(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
            paper=os.environ.get("ALPACA_PAPER", "true").lower() == "true",
        )
    return _trading


def _d() -> OptionHistoricalDataClient:
    global _data
    if _data is None:
        _data = OptionHistoricalDataClient(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
        )
    return _data


def list_expirations(symbol: str, limit: int = 12) -> list[str]:
    req = GetOptionContractsRequest(
        underlying_symbols=[symbol.upper()],
        status=AssetStatus.ACTIVE,
        limit=10000,
    )
    resp = _t().get_option_contracts(req)
    dates = sorted({c.expiration_date.isoformat() for c in resp.option_contracts})
    today = date.today().isoformat()
    future = [d for d in dates if d >= today]
    return future[:limit]


def get_chain(
    symbol: str,
    expiration: str,
    option_type: str | None = None,
    strike_min: float | None = None,
    strike_max: float | None = None,
) -> list[dict]:
    kwargs = dict(
        underlying_symbols=[symbol.upper()],
        status=AssetStatus.ACTIVE,
        expiration_date=date.fromisoformat(expiration),
        limit=10000,
    )
    if option_type:
        kwargs["type"] = ContractType.CALL if option_type.lower().startswith("c") else ContractType.PUT
    if strike_min is not None:
        kwargs["strike_price_gte"] = str(strike_min)
    if strike_max is not None:
        kwargs["strike_price_lte"] = str(strike_max)

    resp = _t().get_option_contracts(GetOptionContractsRequest(**kwargs))
    contracts = resp.option_contracts
    if not contracts:
        return []

    contracts.sort(key=lambda c: (c.type.value, float(c.strike_price)))
    symbols = [c.symbol for c in contracts]

    snapshots = {}
    for i in range(0, len(symbols), 100):
        batch = symbols[i:i + 100]
        snap_resp = _d().get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=batch))
        snapshots.update(snap_resp)

    out = []
    for c in contracts[:20]:
        snap = snapshots.get(c.symbol)
        q = snap.latest_quote if snap else None
        g = snap.greeks if snap else None
        out.append({
            "sym": c.symbol,
            "type": c.type.value,
            "K": round(float(c.strike_price), 2),
            "bid": round(float(q.bid_price), 2) if q else None,
            "ask": round(float(q.ask_price), 2) if q else None,
            "iv": round(float(snap.implied_volatility), 3) if snap and snap.implied_volatility else None,
            "d": round(float(g.delta), 3) if g and g.delta is not None else None,
            "th": round(float(g.theta), 4) if g and g.theta is not None else None,
            "oi": int(c.open_interest) if c.open_interest else None,
        })
    return out


def get_contract_snapshot(option_symbol: str) -> dict:
    resp = _d().get_option_snapshot(
        OptionSnapshotRequest(symbol_or_symbols=option_symbol.upper())
    )
    snap = resp.get(option_symbol.upper())
    if not snap:
        return {"symbol": option_symbol, "error": "no snapshot available"}
    q = snap.latest_quote
    g = snap.greeks
    return {
        "symbol": option_symbol.upper(),
        "bid": float(q.bid_price) if q else None,
        "ask": float(q.ask_price) if q else None,
        "last": float(snap.latest_trade.price) if snap.latest_trade else None,
        "iv": float(snap.implied_volatility) if snap.implied_volatility else None,
        "delta": float(g.delta) if g and g.delta is not None else None,
        "gamma": float(g.gamma) if g and g.gamma is not None else None,
        "theta": float(g.theta) if g and g.theta is not None else None,
        "vega": float(g.vega) if g and g.vega is not None else None,
        "rho": float(g.rho) if g and g.rho is not None else None,
    }
