import os

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce


_client: TradingClient | None = None


def _get_client() -> TradingClient:
    global _client
    if _client is None:
        _client = TradingClient(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
            paper=os.environ.get("ALPACA_PAPER", "true").lower() == "true",
        )
    return _client


def get_account() -> dict:
    a = _get_client().get_account()
    return {
        "account_number": a.account_number,
        "status": str(a.status),
        "cash": float(a.cash),
        "buying_power": float(a.buying_power),
        "portfolio_value": float(a.portfolio_value),
        "equity": float(a.equity),
        "pattern_day_trader": a.pattern_day_trader,
    }


def get_positions() -> list[dict]:
    positions = _get_client().get_all_positions()
    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc),
            "current_price": float(p.current_price),
        }
        for p in positions
    ]


def submit_order(
    symbol: str,
    qty: float,
    side: str,
    order_type: str = "market",
    limit_price: float | None = None,
    time_in_force: str = "day",
) -> dict:
    side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    tif_enum = TimeInForce(time_in_force.lower())

    if order_type.lower() == "limit":
        if limit_price is None:
            raise ValueError("limit_price required for limit orders")
        req = LimitOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=side_enum,
            limit_price=limit_price,
            time_in_force=tif_enum,
        )
    else:
        req = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=side_enum,
            time_in_force=tif_enum,
        )

    order = _get_client().submit_order(req)
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "qty": float(order.qty),
        "side": str(order.side),
        "type": str(order.order_type),
        "status": str(order.status),
        "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
    }


def get_orders(status: str = "open", limit: int = 20) -> list[dict]:
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    req = GetOrdersRequest(
        status=QueryOrderStatus(status.lower()),
        limit=limit,
    )
    orders = _get_client().get_orders(filter=req)
    return [
        {
            "id": str(o.id),
            "symbol": o.symbol,
            "qty": float(o.qty),
            "side": str(o.side),
            "type": str(o.order_type),
            "status": str(o.status),
            "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
        }
        for o in orders
    ]
