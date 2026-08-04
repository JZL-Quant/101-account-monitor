from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .models import FuturesPosition, HedgeRow, SpotBalance, SpotSymbol, ZERO, decimal_value


TRADE_ACCOUNT_TYPES = {"trade", "trade_hf", "unified"}
NO_MARGIN_REQUIREMENT_RATIO = Decimal("100")
MMR_STOP_OPENING_THRESHOLD = Decimal("6.0")
IMR_STOP_OPENING_THRESHOLD = Decimal("1.2")
MMR_RESUME_OPENING_THRESHOLD = Decimal("6.44")
IMR_RESUME_OPENING_THRESHOLD = Decimal("1.77")


def calculate_margin_rates(account: dict[str, Any]) -> tuple[Decimal, Decimal]:
    risk_ratio = decimal_value(account.get("riskRatio"))
    adjusted_equity = decimal_value(account.get("adjustedEquity"))
    initial_margin = decimal_value(account.get("im"))
    maintenance_margin = decimal_value(account.get("mm"))
    if risk_ratio > ZERO:
        mmr = Decimal("1") / risk_ratio
    elif maintenance_margin == ZERO:
        mmr = NO_MARGIN_REQUIREMENT_RATIO
    else:
        mmr = adjusted_equity / maintenance_margin
    imr = (
        NO_MARGIN_REQUIREMENT_RATIO
        if initial_margin == ZERO
        else adjusted_equity / initial_margin
    )
    return mmr, imr


def calculate_opening_risk_gate(mmr: Decimal, imr: Decimal) -> dict[str, Any]:
    is_risky = mmr < MMR_STOP_OPENING_THRESHOLD or imr < IMR_STOP_OPENING_THRESHOLD
    is_safe = mmr > MMR_RESUME_OPENING_THRESHOLD and imr > IMR_RESUME_OPENING_THRESHOLD
    if is_risky:
        result = "禁止开仓"
        detail = "触发停止开仓条件"
    elif is_safe:
        result = "允许恢复开仓"
        detail = "满足恢复开仓条件"
    else:
        result = "保持当前开仓状态"
        detail = "处于滞回区间，交易程序不会改变当前状态"
    return {
        "is_risky": is_risky,
        "is_safe": is_safe,
        "result": result,
        "detail": detail,
        "stop_condition": "MMR < 6.0 或 IMR < 1.2",
        "resume_condition": "MMR > 6.44 且 IMR > 1.77",
    }


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
    excluded_assets: frozenset[str] | set[str] | None = None,
) -> list[HedgeRow]:
    spot_qty: dict[str, Decimal] = defaultdict(lambda: ZERO)
    spot_available: dict[str, Decimal] = defaultdict(lambda: ZERO)
    futures_qty: dict[str, Decimal] = defaultdict(lambda: ZERO)
    futures_symbols: dict[str, list[str]] = defaultdict(list)

    for balance in spot_balances:
        asset = normalize_asset(balance.currency, aliases)
        if asset == quote_currency:
            continue
        # KuCoin UTA 的 balance 已经带方向，liability 是负债明细，不能再次相减。
        # equity 是平台给出的扣除负债/利息后的净资产口径，直接用于现货敞口。
        spot_qty[asset] += balance.equity
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
    excluded = excluded_assets or frozenset()
    for asset in sorted(set(spot_qty) | set(futures_qty)):
        spot = spot_qty[asset]
        future = futures_qty[asset]
        net = spot + future
        price = prices.get(asset, ZERO)
        net_value = net * price
        denominator = max(abs(spot), abs(future))
        mismatch = abs(net) / denominator * Decimal("100") if denominator else ZERO
        same_direction = spot != ZERO and future != ZERO and (spot > 0) == (future > 0)

        is_excluded = asset in excluded
        if is_excluded:
            mismatch = ZERO
            status = "现货储备"
        elif abs(net_value) < dust:
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
                excluded_from_hedge=is_excluded,
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
