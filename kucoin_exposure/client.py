from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .config import KucoinConfig
from .calculator import check_position_conversion
from .models import FuturesPosition, SpotBalance, SpotSymbol, ZERO, decimal_value


UTA_BASE_URL = "https://api.kucoin.com"
# UTA 的公开 instrument 返回字段仍在快速迭代。合约 multiplier 暂用 KuCoin
# 经典公开合约元数据交叉补齐；该请求不涉及账户模式或 API Key。
FUTURES_PUBLIC_BASE_URL = "https://api-futures.kucoin.com"
UTA_KEY_VERSION = "3"


class KucoinAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        status: int | None = None,
        uncertain: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.uncertain = uncertain


class KucoinClient:
    def __init__(self, config: KucoinConfig, logger=None):
        self.config = config
        self.logger = logger
        self._session: aiohttp.ClientSession | None = None
        self._contract_cache: dict[str, dict[str, Any]] = {}
        self._spot_symbols_cache: dict[str, SpotSymbol] | None = None
        self._private_request_lock = asyncio.Lock()
        self._last_private_request_at = 0.0
        # UTA 子账户默认限频较低。串行化私有 REST 请求并留出间隔，
        # 避免页面刷新、定时刷新和平仓流程在同一秒内形成突发请求。
        self._private_request_interval = 0.15

    async def start(self):
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10, connect=5)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        await self.start()
        assert self._session is not None
        return self._session

    @staticmethod
    def _encode_query(params: dict[str, Any] | None) -> str:
        if not params:
            return ""
        return urlencode(
            [(key, value) for key, value in params.items() if value is not None]
        )

    def _private_headers(
        self, method: str, endpoint: str, body_text: str
    ) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        prehash = f"{timestamp}{method.upper()}{endpoint}{body_text}"
        secret = self.config.api_secret.encode()
        signature = base64.b64encode(
            hmac.new(secret, prehash.encode(), hashlib.sha256).digest()
        ).decode()
        passphrase = base64.b64encode(
            hmac.new(
                secret, self.config.api_passphrase.encode(), hashlib.sha256
            ).digest()
        ).decode()
        return {
            "KC-API-KEY": self.config.api_key,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": passphrase,
            "KC-API-KEY-VERSION": UTA_KEY_VERSION,
            "Content-Type": "application/json",
            "X-SITE-TYPE": self.config.site_type,
        }

    async def _request(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = True,
        order_request: bool = False,
    ) -> Any:
        if private:
            async with self._private_request_lock:
                elapsed = time.monotonic() - self._last_private_request_at
                if elapsed < self._private_request_interval:
                    await asyncio.sleep(self._private_request_interval - elapsed)
                try:
                    return await self._request_impl(
                        method,
                        base_url,
                        path,
                        params=params,
                        body=body,
                        private=private,
                        order_request=order_request,
                    )
                finally:
                    self._last_private_request_at = time.monotonic()
        return await self._request_impl(
            method,
            base_url,
            path,
            params=params,
            body=body,
            private=private,
            order_request=order_request,
        )

    async def _request_impl(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        private: bool,
        order_request: bool,
    ) -> Any:
        query = self._encode_query(params)
        endpoint = path + (f"?{query}" if query else "")
        body_text = (
            json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            if body is not None
            else ""
        )
        session = await self._get_session()
        # GET 和 cancel-all 都是可安全重复的；真实下单绝不自动重试，
        # 防止响应丢失时生成重复成交。
        retry_rate_limit = method.upper() == "GET" or path.endswith("/cancel-all")
        max_attempts = 4 if retry_rate_limit else 1
        for attempt in range(max_attempts):
            headers = (
                self._private_headers(method, endpoint, body_text)
                if private
                else {"Content-Type": "application/json"}
            )
            try:
                async with session.request(
                    method.upper(),
                    base_url + endpoint,
                    headers=headers,
                    data=body_text or None,
                ) as response:
                    text = await response.text()
                    response_headers = response.headers
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise KucoinAPIError(
                            f"KuCoin returned non-JSON response (HTTP {response.status})",
                            status=response.status,
                            uncertain=order_request,
                        ) from exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise KucoinAPIError(
                    f"KuCoin request failed: {exc}", uncertain=order_request
                ) from exc

            code = str(payload.get("code", ""))
            if (
                code == "429000"
                and retry_rate_limit
                and attempt + 1 < max_attempts
            ):
                reset_text = response_headers.get("gw-ratelimit-reset", "")
                try:
                    reset_seconds = max(0.0, float(reset_text) / 1000)
                except (TypeError, ValueError):
                    reset_seconds = 0.0
                # 正常配额耗尽按响应头等待；服务过载没有响应头时逐步退避。
                delay = (
                    min(reset_seconds, 30.0) + 0.25
                    if reset_seconds
                    else 3.0 * (2**attempt)
                )
                if self.logger:
                    self.logger.warning(
                        "KuCoin rate limit for %s; retry %s/%s in %.2fs",
                        path,
                        attempt + 1,
                        max_attempts - 1,
                        delay,
                    )
                await asyncio.sleep(delay)
                continue

            if response.status >= 400 or code != "200000":
                message = str(payload.get("msg") or payload.get("message") or text)
                raise KucoinAPIError(
                    f"KuCoin error {code or response.status}: {message}",
                    code=code,
                    status=response.status,
                )
            return payload.get("data")

        raise AssertionError("unreachable")

    async def validate_credentials(self) -> str:
        data = await self._request(
            "GET",
            UTA_BASE_URL,
            "/api/ua/v1/unified/account/balance",
        )
        account_type = str((data or {}).get("accountType", "")).upper()
        if account_type and account_type != "UNIFIED":
            raise KucoinAPIError(
                f"Expected UTA accountType=UNIFIED, got {account_type}"
            )
        return "UTA/V3"

    async def fetch_spot_balances(self) -> list[SpotBalance]:
        data = await self._request(
            "GET", UTA_BASE_URL, "/api/ua/v1/unified/account/balance"
        )
        result: list[SpotBalance] = []
        for account in (data or {}).get("accounts", []):
            account_type = str(
                account.get("accountType") or data.get("accountType") or "UNIFIED"
            ).lower()
            for item in account.get("currencies", []):
                balance = decimal_value(item.get("balance"))
                liability = decimal_value(item.get("liability"))
                equity = decimal_value(item.get("equity"), balance - liability)
                if balance == ZERO and liability == ZERO and equity == ZERO:
                    continue
                result.append(
                    SpotBalance(
                        currency=str(item.get("currency", "")).upper(),
                        account_type=account_type,
                        balance=balance,
                        available=decimal_value(item.get("available")),
                        holds=decimal_value(item.get("hold")),
                        liability=liability,
                        equity=equity,
                    )
                )
        return result

    async def fetch_spot_symbols(self, *, force=False) -> dict[str, SpotSymbol]:
        if self._spot_symbols_cache is not None and not force:
            return self._spot_symbols_cache
        data = await self._request(
            "GET",
            UTA_BASE_URL,
            "/api/ua/v1/market/instrument",
            params={"tradeType": "SPOT"},
            private=False,
        )
        symbols = {}
        for item in (data or {}).get("list", []):
            symbol = str(item.get("symbol", "")).upper()
            symbols[symbol] = SpotSymbol(
                symbol=symbol,
                base_currency=str(item.get("baseCurrency", "")).upper(),
                quote_currency=str(item.get("quoteCurrency", "")).upper(),
                base_increment=decimal_value(item.get("baseOrderStep")),
                base_min_size=decimal_value(item.get("minBaseOrderSize")),
                min_funds=decimal_value(
                    item.get("minFunds") or item.get("minQuoteOrderSize")
                ),
                enabled=str(item.get("tradingStatus", "0")) == "1",
            )
        self._spot_symbols_cache = symbols
        return symbols

    async def fetch_spot_prices(self, quote_currency: str) -> dict[str, Decimal]:
        data = await self._request(
            "GET",
            UTA_BASE_URL,
            "/api/ua/v1/market/ticker",
            params={"tradeType": "SPOT"},
            private=False,
        )
        prices = {}
        suffix = f"-{quote_currency}"
        for ticker in (data or {}).get("list", []):
            symbol = str(ticker.get("symbol", "")).upper()
            if symbol.endswith(suffix):
                prices[symbol[: -len(suffix)]] = decimal_value(
                    ticker.get("lastPrice")
                )
        return prices

    async def fetch_contract(self, symbol: str) -> dict[str, Any]:
        if symbol not in self._contract_cache:
            self._contract_cache[symbol] = await self._request(
                "GET",
                FUTURES_PUBLIC_BASE_URL,
                f"/api/v1/contracts/{symbol}",
                private=False,
            )
        return self._contract_cache[symbol]

    async def fetch_futures_positions(
        self, settle_currency: str
    ) -> list[FuturesPosition]:
        data = await self._request(
            "GET",
            UTA_BASE_URL,
            "/api/ua/v1/unified/position/open-list",
            params={"pageNumber": 1, "pageSize": 200},
        )
        raw_items = data if isinstance(data, list) else (data or {}).get("items", [])
        raw_positions = [
            item
            for item in raw_items
            if decimal_value(item.get("size")) != ZERO
        ]
        contracts = await asyncio.gather(
            *(self.fetch_contract(str(item["symbol"])) for item in raw_positions)
        )
        positions = []
        for item, contract in zip(raw_positions, contracts):
            signed_contracts = decimal_value(item.get("size"))
            multiplier = decimal_value(contract.get("multiplier"))
            base_qty = signed_contracts * multiplier
            mark_price = decimal_value(item.get("markPrice"))
            position_value = abs(decimal_value(item.get("positionValue")))
            (
                calculated_value,
                value_error_percent,
                conversion_ok,
            ) = check_position_conversion(
                signed_contracts,
                multiplier,
                mark_price,
                position_value,
            )
            positions.append(
                FuturesPosition(
                    symbol=str(item.get("symbol", "")),
                    base_currency=str(contract.get("baseCurrency", "")).upper(),
                    settle_currency=str(
                        item.get("settleCurrency")
                        or contract.get("settleCurrency", settle_currency)
                    ).upper(),
                    side="long" if signed_contracts > ZERO else "short",
                    current_qty=signed_contracts,
                    multiplier=multiplier,
                    base_qty=base_qty,
                    mark_price=mark_price,
                    mark_value=position_value,
                    avg_entry_price=decimal_value(item.get("entryPrice")),
                    liquidation_price=decimal_value(item.get("liquidationPrice")),
                    unrealised_pnl=decimal_value(item.get("unrealizedPnL")),
                    leverage=decimal_value(item.get("leverage")),
                    margin_mode=str(item.get("marginMode", "")).upper(),
                    position_side=str(item.get("positionSide", "BOTH")).upper(),
                    is_inverse=bool(contract.get("isInverse", False)),
                    calculated_mark_value=calculated_value,
                    conversion_error_percent=value_error_percent,
                    conversion_ok=conversion_ok,
                    per_contract_mark_value=abs(multiplier * mark_price),
                )
            )
        return positions

    async def fetch_futures_account(self, currency: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            UTA_BASE_URL,
            "/api/ua/v1/unified/account/overview",
        )

    async def _cancel_orders(self, symbol: str, trade_type: str) -> Any:
        body: dict[str, Any] = {
            "symbol": symbol,
            "tradeType": trade_type,
            "orderFilter": "NORMAL",
        }
        if trade_type == "FUTURES":
            body["marginMode"] = "CROSS"
        return await self._request(
            "POST",
            UTA_BASE_URL,
            "/api/ua/v1/unified/order/cancel-all",
            body=body,
            order_request=True,
        )

    async def cancel_spot_orders(self, symbol: str) -> Any:
        return await self._cancel_orders(symbol, "SPOT")

    async def cancel_futures_orders(self, symbol: str) -> Any:
        return await self._cancel_orders(symbol, "FUTURES")

    async def place_spot_market_sell(self, symbol: str, size: Decimal) -> dict:
        client_oid = uuid.uuid4().hex
        data = await self._request(
            "POST",
            UTA_BASE_URL,
            "/api/ua/v1/unified/order/place",
            body={
                "tradeType": "SPOT",
                "symbol": symbol,
                "clientOid": client_oid,
                "side": "SELL",
                "orderType": "MARKET",
                "size": format(size, "f"),
                "sizeUnit": "BASECCY",
                "postOnly": False,
                "reduceOnly": False,
                "timeInForce": "GTC",
                "tags": "hedge-close",
            },
            order_request=True,
        )
        return {"clientOid": client_oid, **(data or {})}

    async def place_futures_close(self, position: FuturesPosition) -> dict:
        if not position.conversion_ok:
            raise ValueError(
                f"{position.symbol} multiplier 换算校验未通过，拒绝自动平仓"
            )
        client_oid = uuid.uuid4().hex
        data = await self._request(
            "POST",
            UTA_BASE_URL,
            "/api/ua/v1/unified/order/place",
            body={
                "tradeType": "FUTURES",
                "symbol": position.symbol,
                "clientOid": client_oid,
                "side": "SELL" if position.base_qty > ZERO else "BUY",
                "orderType": "MARKET",
                "size": format(abs(position.current_qty), "f"),
                "sizeUnit": "UNIT",
                "postOnly": False,
                "reduceOnly": True,
                "timeInForce": "GTC",
                "tags": "hedge-close",
            },
            order_request=True,
        )
        return {"clientOid": client_oid, **(data or {})}
