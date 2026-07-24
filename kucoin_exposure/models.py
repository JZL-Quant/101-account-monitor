from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


ZERO = Decimal("0")


def decimal_value(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


@dataclass(frozen=True)
class SpotBalance:
    currency: str
    account_type: str
    balance: Decimal
    available: Decimal
    holds: Decimal
    liability: Decimal = ZERO
    equity: Decimal = ZERO


@dataclass(frozen=True)
class SpotSymbol:
    symbol: str
    base_currency: str
    quote_currency: str
    base_increment: Decimal
    base_min_size: Decimal
    min_funds: Decimal
    enabled: bool


@dataclass(frozen=True)
class FuturesPosition:
    symbol: str
    base_currency: str
    settle_currency: str
    side: str
    current_qty: Decimal
    multiplier: Decimal
    base_qty: Decimal
    mark_price: Decimal
    mark_value: Decimal
    avg_entry_price: Decimal
    liquidation_price: Decimal
    unrealised_pnl: Decimal
    leverage: Decimal
    margin_mode: str
    position_side: str
    is_inverse: bool
    calculated_mark_value: Decimal = ZERO
    conversion_error_percent: Decimal = ZERO
    conversion_ok: bool = False
    per_contract_mark_value: Decimal = ZERO


@dataclass(frozen=True)
class HedgeRow:
    asset: str
    spot_qty: Decimal
    spot_trade_available: Decimal
    futures_qty: Decimal
    net_qty: Decimal
    price: Decimal
    net_value: Decimal
    mismatch_percent: Decimal
    status: str
    futures_symbols: tuple[str, ...]
    spot_symbol: str | None
    excluded_from_hedge: bool = False


def json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, tuple):
        return list(value)
    if hasattr(value, "__dataclass_fields__"):
        return {key: json_ready(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value
