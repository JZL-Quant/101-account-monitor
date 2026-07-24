from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .calculator import build_hedge_rows, floor_to_increment, normalize_asset
from .client import KucoinAPIError, KucoinClient
from .config import AppConfig
from .models import (
    FuturesPosition,
    HedgeRow,
    SpotBalance,
    SpotSymbol,
    ZERO,
    json_ready,
)
from .repository import ExposureRepository


LOGGER = logging.getLogger("kucoin_exposure")


class ExposureService:
    def __init__(
        self,
        config: AppConfig,
        client: KucoinClient,
        repository: ExposureRepository,
    ):
        self.config = config
        self.client = client
        self.repository = repository
        # create_app() 在 uvicorn event loop 启动前运行，锁延迟到 start()。
        self._refresh_lock: asyncio.Lock | None = None
        self._asset_locks: dict[str, asyncio.Lock] = {}
        self._latest: dict[str, Any] | None = None

    async def start(self):
        self._refresh_lock = asyncio.Lock()
        self._asset_locks.clear()
        await self.repository.initialize()
        await self.client.start()
        self._latest = await self.repository.latest_success()

    async def close(self):
        await self.client.close()
        self._refresh_lock = None
        self._asset_locks.clear()

    def _asset_lock(self, asset: str) -> asyncio.Lock:
        return self._asset_locks.setdefault(asset, asyncio.Lock())

    async def _fetch_state(
        self,
    ) -> tuple[
        list[SpotBalance],
        list[FuturesPosition],
        dict[str, SpotSymbol],
        list[HedgeRow],
        dict[str, Any],
    ]:
        quote = self.config.hedge.quote_currency
        spot_balances, futures_positions, spot_symbols, spot_prices, account = (
            await asyncio.gather(
                self.client.fetch_spot_balances(),
                self.client.fetch_futures_positions(quote),
                self.client.fetch_spot_symbols(),
                self.client.fetch_spot_prices(quote),
                self.client.fetch_futures_account(quote),
            )
        )

        prices = {
            normalize_asset(asset, self.config.hedge.aliases): price
            for asset, price in spot_prices.items()
        }
        for position in futures_positions:
            asset = normalize_asset(
                position.base_currency, self.config.hedge.aliases
            )
            if position.mark_price > ZERO:
                prices[asset] = position.mark_price

        hedge_rows = build_hedge_rows(
            spot_balances,
            futures_positions,
            spot_symbols,
            prices,
            aliases=self.config.hedge.aliases,
            quote_currency=quote,
            matched_threshold_percent=self.config.hedge.matched_threshold_percent,
            warning_threshold_percent=self.config.hedge.warning_threshold_percent,
            dust_value_usdt=self.config.hedge.dust_value_usdt,
        )
        return spot_balances, futures_positions, spot_symbols, hedge_rows, account

    def _build_payload(
        self,
        spot_balances: list[SpotBalance],
        futures_positions: list[FuturesPosition],
        hedge_rows: list[HedgeRow],
        futures_account: dict[str, Any],
    ) -> dict[str, Any]:
        long_exposure = sum(
            (position.mark_value for position in futures_positions if position.base_qty > 0),
            ZERO,
        )
        short_exposure = sum(
            (position.mark_value for position in futures_positions if position.base_qty < 0),
            ZERO,
        )
        payload = {
            "sampled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": {
                "account_equity": futures_account.get("equity", 0),
                "available_balance": futures_account.get(
                    "availableMargin", 0
                ),
                "unrealised_pnl": sum(
                    (position.unrealised_pnl for position in futures_positions),
                    ZERO,
                ),
                "long_exposure": long_exposure,
                "short_exposure": short_exposure,
                "net_futures_exposure": long_exposure - short_exposure,
                "conversion_all_ok": all(
                    position.conversion_ok for position in futures_positions
                ),
            },
            "hedges": hedge_rows,
            "spot_balances": spot_balances,
            "futures_positions": futures_positions,
        }
        return json_ready(payload)

    async def refresh(self) -> dict[str, Any]:
        if self._refresh_lock is None:
            raise RuntimeError("ExposureService has not been started")
        if self._refresh_lock.locked():
            LOGGER.warning("Skipping overlapping KuCoin exposure refresh")
            return self._latest or {"status": "refresh_in_progress"}
        async with self._refresh_lock:
            try:
                (
                    spot_balances,
                    futures_positions,
                    _spot_symbols,
                    hedge_rows,
                    account,
                ) = await self._fetch_state()
                payload = self._build_payload(
                    spot_balances, futures_positions, hedge_rows, account
                )
                await self.repository.save_success(
                    payload,
                    json_ready(spot_balances),
                    json_ready(futures_positions),
                    json_ready(hedge_rows),
                )
                self._latest = payload
                return payload
            except Exception as exc:
                LOGGER.exception("KuCoin exposure refresh failed")
                await self.repository.save_failure(str(exc))
                raise

    async def latest(self) -> dict[str, Any]:
        payload = self._latest or await self.repository.latest_success()
        attempt = await self.repository.last_attempt()
        actions = await self.repository.recent_trade_actions()
        if payload is None:
            payload = {
                "sampled_at": None,
                "summary": {},
                "hedges": [],
                "spot_balances": [],
                "futures_positions": [],
            }
        sampled_at = payload.get("sampled_at")
        stale = True
        if sampled_at:
            timestamp = datetime.fromisoformat(sampled_at)
            age = (datetime.now(timezone.utc) - timestamp).total_seconds()
            stale = age > self.config.monitor.stale_after_seconds
        return {
            **payload,
            "stale": stale,
            "last_attempt": attempt,
            "trading_enabled": self.config.trading.enabled,
            "actions": actions,
        }

    async def preview_close(self, asset: str) -> dict[str, Any]:
        asset = normalize_asset(asset, self.config.hedge.aliases)
        # 预览只读取最后一次成功快照，不额外消耗 KuCoin REST 配额。
        latest = await self.latest()
        row = next(
            (item for item in latest.get("hedges", []) if item["asset"] == asset),
            None,
        )
        if row is None:
            raise ValueError(f"{asset} 当前没有现货或合约敞口")
        return {
            "asset": asset,
            "spot_qty": row["spot_qty"],
            "spot_trade_available": row["spot_trade_available"],
            "futures_qty": row["futures_qty"],
            "net_qty": row["net_qty"],
            "net_value": row["net_value"],
            "spot_symbol": row["spot_symbol"],
            "futures_symbols": row["futures_symbols"],
            "sampled_at": latest.get("sampled_at"),
            "stale": latest.get("stale", True),
            "warning": "预览来自最近快照；确认后会重新查询并按最新数量平仓。",
        }

    async def close_both_sides(self, asset: str, username: str) -> dict[str, Any]:
        if not self.config.trading.enabled:
            raise PermissionError("config.yaml 中 trading.enabled 尚未启用")
        asset = normalize_asset(asset, self.config.hedge.aliases)
        async with self._asset_lock(asset):
            request_record: dict[str, Any] = {"asset": asset}
            result: dict[str, Any] = {"cancel": [], "orders": []}
            try:
                (
                    _balances,
                    positions,
                    spot_symbols,
                    rows,
                    _account,
                ) = await self._fetch_state()
                row = next((item for item in rows if item.asset == asset), None)
                if row is None:
                    raise ValueError(f"{asset} 当前已经没有可平敞口")
                asset_positions = [
                    position
                    for position in positions
                    if normalize_asset(
                        position.base_currency, self.config.hedge.aliases
                    )
                    == asset
                ]
                request_record.update(json_ready(row))

                if self.config.trading.cancel_open_orders_before_close:
                    cancel_targets: list[tuple[str, str, str]] = []
                    if row.spot_symbol:
                        cancel_targets.append(
                            (f"spot:{row.spot_symbol}", row.spot_symbol, "SPOT")
                        )
                    for symbol in sorted(
                        {position.symbol for position in asset_positions}
                    ):
                        cancel_targets.append(
                            (f"futures:{symbol}", symbol, "FUTURES")
                        )

                    open_order_checks = await asyncio.gather(
                        *(
                            self.client.has_open_orders(symbol, trade_type)
                            for _name, symbol, trade_type in cancel_targets
                        ),
                        return_exceptions=True,
                    )
                    cancel_jobs = []
                    cancel_names = []
                    check_failures = []
                    for target, has_orders in zip(
                        cancel_targets, open_order_checks
                    ):
                        name, symbol, trade_type = target
                        if isinstance(has_orders, Exception):
                            check_failures.append(f"{name}: {has_orders}")
                            continue
                        if not has_orders:
                            result["cancel"].append(
                                {
                                    "leg": name,
                                    "ok": True,
                                    "skipped": True,
                                    "result": "没有普通活动委托，无需撤单",
                                }
                            )
                            continue
                        cancel_names.append(name)
                        cancel_jobs.append(
                            self.client.cancel_spot_orders(symbol)
                            if trade_type == "SPOT"
                            else self.client.cancel_futures_orders(symbol)
                        )

                    if check_failures:
                        raise ValueError(
                            "无法确认活动委托，已停止双边平仓："
                            + "; ".join(check_failures)
                        )
                    cancel_results = await asyncio.gather(
                        *cancel_jobs, return_exceptions=True
                    )
                    cancel_failures = []
                    for name, value in zip(cancel_names, cancel_results):
                        if isinstance(value, Exception):
                            cancel_failures.append(f"{name}: {value}")
                        result["cancel"].append(
                            {
                                "leg": name,
                                "ok": not isinstance(value, Exception),
                                "result": (
                                    str(value)
                                    if isinstance(value, Exception)
                                    else json_ready(value)
                                ),
                            }
                        )
                    if cancel_failures:
                        raise ValueError(
                            "活动委托撤销未全部确认，已停止双边平仓："
                            + "; ".join(cancel_failures)
                        )

                # Cancel acknowledgements can arrive before the balance is released.
                await asyncio.sleep(0.3)
                balances, positions, spot_symbols, rows, _account = (
                    await self._fetch_state()
                )
                row = next((item for item in rows if item.asset == asset), None)
                if row is None:
                    verification = await self.refresh()
                    result["verification"] = verification
                    await self.repository.save_trade_action(
                        username=username,
                        asset=asset,
                        status="already_flat",
                        request=request_record,
                        result=result,
                    )
                    return result

                asset_positions = [
                    position
                    for position in positions
                    if normalize_asset(
                        position.base_currency, self.config.hedge.aliases
                    )
                    == asset
                ]
                invalid_conversions = [
                    position.symbol
                    for position in asset_positions
                    if not position.conversion_ok
                ]
                if invalid_conversions:
                    raise ValueError(
                        "以下合约的 multiplier 换算校验未通过，已拒绝执行双边平仓："
                        + ", ".join(invalid_conversions)
                    )
                order_jobs = []
                order_names = []
                for position in asset_positions:
                    order_jobs.append(self.client.place_futures_close(position))
                    order_names.append(f"futures:{position.symbol}")

                if row.spot_symbol and row.spot_trade_available > ZERO:
                    symbol_info = spot_symbols[row.spot_symbol]
                    sell_size = floor_to_increment(
                        row.spot_trade_available, symbol_info.base_increment
                    )
                    estimated_funds = sell_size * row.price
                    if (
                        sell_size >= symbol_info.base_min_size
                        and estimated_funds >= symbol_info.min_funds
                    ):
                        order_jobs.append(
                            self.client.place_spot_market_sell(
                                row.spot_symbol, sell_size
                            )
                        )
                        order_names.append(f"spot:{row.spot_symbol}")
                    else:
                        result["orders"].append(
                            {
                                "leg": f"spot:{row.spot_symbol}",
                                "ok": False,
                                "error": "可卖余额低于交易对最小下单量，作为残余余额保留",
                            }
                        )

                if order_jobs:
                    order_results = await asyncio.gather(
                        *order_jobs, return_exceptions=True
                    )
                    for name, value in zip(order_names, order_results):
                        if isinstance(value, Exception):
                            result["orders"].append(
                                {
                                    "leg": name,
                                    "ok": False,
                                    "uncertain": bool(
                                        isinstance(value, KucoinAPIError)
                                        and value.uncertain
                                    ),
                                    "error": str(value),
                                }
                            )
                        else:
                            result["orders"].append(
                                {"leg": name, "ok": True, "result": json_ready(value)}
                            )

                await asyncio.sleep(
                    self.config.trading.verification_delay_seconds
                )
                verification = await self.refresh()
                final_row = next(
                    (
                        item
                        for item in verification.get("hedges", [])
                        if item["asset"] == asset
                    ),
                    None,
                )
                result["remaining"] = final_row
                failed = any(not order.get("ok") for order in result["orders"])
                remaining_material = bool(
                    final_row
                    and (
                        abs(float(final_row.get("net_value", 0)))
                        >= self.config.hedge.dust_value_usdt
                        or abs(float(final_row.get("futures_qty", 0))) > 0
                    )
                )
                status = (
                    "complete"
                    if not failed and not remaining_material
                    else "partial_or_failed"
                )
                await self.repository.save_trade_action(
                    username=username,
                    asset=asset,
                    status=status,
                    request=request_record,
                    result=result,
                )
                return {"status": status, **result}
            except Exception as exc:
                LOGGER.exception("Two-sided close failed for %s", asset)
                await self.repository.save_trade_action(
                    username=username,
                    asset=asset,
                    status="failed",
                    request=request_record,
                    result=result,
                    error_message=str(exc),
                )
                raise
