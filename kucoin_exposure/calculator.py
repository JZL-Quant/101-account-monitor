from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .models import FuturesPosition, HedgeRow, SpotBalance, SpotSymbol, ZERO


TRADE_ACCOUNT_TYPES = {"trade", "trade_hf", "unified"}


def normalize_asset(asset: str, aliases: dict[str, str]) -> str:
    normalized = str(asset or "").strip().upper()
    return aliases.get(normalized, normalized)


def build_hedge_rows(
    spot_balances: list[SpotBalance],
    futures_positions: list[FuturesPosition],
    spot_symbols: dict[str, SpotSymbol],
    prices: dict[str, Decimal],
    *,
    aliases: dict[str, str],
    quote_currency: str,
    matched_threshold_percent: float,
    warning_threshold_percent: float,
    dust_value_usdt: float,
) -> list[HedgeRow]:
    spot_qty: dict[str, Decimal] = defaultdict(lambda: ZERO)
    spot_available: dict[str, Decimal] = defaultdict(lambda: ZERO)
    futures_qty: dict[str, Decimal] = defaultdict(lambda: ZERO)
    futures_symbols: dict[str, list[str]] = defaultdict(list)

    for balance in spot_balances:
        asset = normalize_asset(balance.currency, aliases)
        if asset == quote_currency:
            continue
        # UTA 中借入资产会同时增加 balance 和 liability；套利净现货敞口应扣除负债。
        spot_qty[asset] += balance.balance - balance.liability
        if balance.account_type.lower() in TRADE_ACCOUNT_TYPES:
            spot_available[asset] += balance.available

    for position in futures_positions:
        if position.is_inverse:
            continue
        asset = normalize_asset(position.base_currency, aliases)
        futures_qty[asset] += position.base_qty
        futures_symbols[asset].append(position.symbol)

    rows = []
    matched = Decimal(str(matched_threshold_percent))
    warning = Decimal(str(warning_threshold_percent))
    dust = Decimal(str(dust_value_usdt))
    for asset in sorted(set(spot_qty) | set(futures_qty)):
        spot = spot_qty[asset]
        future = futures_qty[asset]
        net = spot + future
        price = prices.get(asset, ZERO)
        net_value = net * price
        denominator = max(abs(spot), abs(future))
        mismatch = abs(net) / denominator * Decimal("100") if denominator else ZERO
        same_direction = spot != ZERO and future != ZERO and (spot > 0) == (future > 0)

        if abs(net_value) < dust:
            # 灰尘余额已按配置忽略，不再显示容易误导的 100% 相对偏差。
            mismatch = ZERO
            status = "已对冲"
        elif same_direction:
            status = "同向暴露"
        elif mismatch <= matched:
            status = "已对冲"
        elif mismatch <= warning:
            status = "轻微偏多" if net > 0 else "轻微偏空"
        else:
            status = "未对冲偏多" if net > 0 else "未对冲偏空"

        spot_symbol = f"{asset}-{quote_currency}"
        if spot_symbol not in spot_symbols or not spot_symbols[spot_symbol].enabled:
            spot_symbol = None
        rows.append(
            HedgeRow(
                asset=asset,
                spot_qty=spot,
                spot_trade_available=spot_available[asset],
                futures_qty=future,
                net_qty=net,
                price=price,
                net_value=net_value,
                mismatch_percent=mismatch,
                status=status,
                futures_symbols=tuple(sorted(set(futures_symbols[asset]))),
                spot_symbol=spot_symbol,
            )
        )
    return rows


def floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if value <= ZERO or increment <= ZERO:
        return ZERO
    return (value // increment) * increment


def check_position_conversion(
    contracts: Decimal,
    multiplier: Decimal,
    mark_price: Decimal,
    reported_position_value: Decimal,
    *,
    tolerance_percent: Decimal = Decimal("0.5"),
) -> tuple[Decimal, Decimal, bool]:
    """Cross-check contract conversion against KuCoin's independent positionValue."""
    calculated_value = abs(contracts * multiplier * mark_price)
    reported_value = abs(reported_position_value)
    if multiplier <= ZERO or mark_price <= ZERO or reported_value <= ZERO:
        return calculated_value, ZERO, False
    error_percent = (
        abs(calculated_value - reported_value)
        / reported_value
        * Decimal("100")
    )
    return calculated_value, error_percent, error_percent <= tolerance_percent
