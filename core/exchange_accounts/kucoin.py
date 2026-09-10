import asyncio
import base64
import hashlib
import hmac
import json
import math
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from .base import BaseExchangeAccount, CredentialField, RUNTIME_LOGGER


KUCOIN_API_BASE_URL = "https://api.kucoin.com"
ACCOUNT_BALANCE_PATH = "/api/ua/v2/unified/account/balance"
MARKET_TICKER_PATH = "/api/ua/v2/market/ticker"
API_KEY_INFO_PATH = "/api/ua/v2/user/api-key"
SUPPORTED_API_KEY_VERSIONS = ("3", "2")
PRICE_BRIDGE_CURRENCIES = ("USDT", "USDC", "BTC", "ETH")


class KucoinAPIError(RuntimeError):
    pass


class KucoinExchangeAccount(BaseExchangeAccount):
    """KuCoin 账户净值适配器，按配置本位币读取账户总估值。"""

    exchange_id = "kucoin"
    exchange_label = "KuCoin"
    supported_account_types = ("account", "account_pro")
    credential_fields = (CredentialField.passphrase(),)
    managed_credential_fields = ("api_key_version",)

    @classmethod
    def from_account_info(cls, name: str, account_info: dict):
        return cls(
            name=name,
            api_key=account_info["key"],
            secret=account_info["secret"],
            passphrase=account_info.get("passphrase", ""),
            initial_unit=account_info["initial_unit"],
            account_type=account_info["account_type"],
            ccy=account_info.get("ccy", "USDT"),
            exchange=account_info.get("exchange", "KuCoin"),
            minute_snapshot_file=account_info.get("minute_snapshot_file"),
            api_key_version=account_info.get("api_key_version"),
            site_type=account_info.get("site_type", "global"),
        )

    def __init__(
        self,
        name,
        api_key,
        secret,
        passphrase,
        initial_unit,
        account_type,
        ccy="USDT",
        exchange="KuCoin",
        minute_snapshot_file=None,
        api_key_version=None,
        site_type="global",
    ):
        super().__init__(
            name,
            api_key,
            secret,
            initial_unit,
            account_type,
            ccy,
            exchange,
            minute_snapshot_file,
        )
        self.passphrase = str(passphrase or "").strip()
        if not self.passphrase:
            raise ValueError(f"KuCoin account {name} requires passphrase")
        self.api_key_version = str(api_key_version or "").strip() or None
        if self.api_key_version is not None and self.api_key_version not in {"2", "3"}:
            raise ValueError("KuCoin api_key_version must be 2 or 3")
        self.site_type = str(site_type or "global").strip() or "global"
        self._api_key_version_lock = asyncio.Lock()

    def _private_headers(
        self, method, endpoint, body="", timestamp_ms=None, api_key_version=None
    ):
        timestamp = str(timestamp_ms or int(time.time() * 1000))
        api_key_version = str(api_key_version or self.api_key_version or "").strip()
        if api_key_version not in {"2", "3"}:
            raise KucoinAPIError("KuCoin API key version has not been detected")
        secret = self.secret.encode("utf-8")
        signature = base64.b64encode(
            hmac.new(
                secret,
                f"{timestamp}{method.upper()}{endpoint}{body}".encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        encrypted_passphrase = base64.b64encode(
            hmac.new(secret, self.passphrase.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        return {
            "KC-API-KEY": self.api_key,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": encrypted_passphrase,
            "KC-API-KEY-VERSION": api_key_version,
            "Content-Type": "application/json",
            "X-SITE-TYPE": self.site_type,
        }

    def _request_sync(self, method, path, params=None, api_key_version=None):
        query = urlencode(params or {})
        endpoint = path + (f"?{query}" if query else "")
        request = urllib.request.Request(
            KUCOIN_API_BASE_URL + endpoint,
            headers=self._private_headers(
                method, endpoint, api_key_version=api_key_version
            ),
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise KucoinAPIError(f"KuCoin HTTP {exc.code}: {body[:300]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise KucoinAPIError(f"KuCoin request failed: {exc}") from exc

        if str(payload.get("code", "")) != "200000":
            message = payload.get("msg") or payload.get("message") or "unknown error"
            raise KucoinAPIError(f"KuCoin error {payload.get('code')}: {message}")
        return payload.get("data") or {}

    def _public_request_sync(self, path, params=None):
        query = urlencode(params or {})
        endpoint = path + (f"?{query}" if query else "")
        request = urllib.request.Request(
            KUCOIN_API_BASE_URL + endpoint,
            headers={"Content-Type": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise KucoinAPIError(f"KuCoin HTTP {exc.code}: {body[:300]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise KucoinAPIError(f"KuCoin request failed: {exc}") from exc

        if str(payload.get("code", "")) != "200000":
            message = payload.get("msg") or payload.get("message") or "unknown error"
            raise KucoinAPIError(f"KuCoin error {payload.get('code')}: {message}")
        return payload.get("data") or {}

    async def request(self, path, api=None, method="GET", params=None, **_kwargs):
        await self.ensure_api_key_version()
        return await asyncio.to_thread(self._request_sync, method, path, params)

    async def ensure_api_key_version(self):
        if self.api_key_version is not None:
            return self.api_key_version
        async with self._api_key_version_lock:
            if self.api_key_version is not None:
                return self.api_key_version
            failures = []
            for candidate in SUPPORTED_API_KEY_VERSIONS:
                try:
                    data = await asyncio.to_thread(
                        self._request_sync,
                        "GET",
                        API_KEY_INFO_PATH,
                        None,
                        candidate,
                    )
                    detected = str(data.get("apiVersion") or candidate)
                    if detected not in SUPPORTED_API_KEY_VERSIONS:
                        raise KucoinAPIError(
                            f"KuCoin returned unsupported apiVersion {detected}"
                        )
                    self.api_key_version = detected
                    RUNTIME_LOGGER.info(
                        "[%s] detected KuCoin API key version %s",
                        self.name,
                        detected,
                    )
                    return detected
                except Exception as exc:
                    failures.append(f"v{candidate}: {exc}")
            raise KucoinAPIError(
                "Unable to detect KuCoin API key version (" + "; ".join(failures) + ")"
            )

    async def fetch_account_balances(self):
        return await self.request(
            ACCOUNT_BALANCE_PATH,
            method="GET",
        )

    async def fetch_spot_tickers(self):
        return await asyncio.to_thread(
            self._public_request_sync,
            MARKET_TICKER_PATH,
            {"tradeType": "SPOT"},
        )

    async def fetch_account_assets(self, account_type: str):
        return await self.fetch_account_balances()

    async def validate_credentials(self):
        await self.ensure_api_key_version()
        await self.fetch_account_balances()
        return {"api_key_version": self.api_key_version}

    @staticmethod
    def _currency_equities(data):
        account_type = str(data.get("accountType") or "").upper()
        if account_type and account_type != "UNIFIED":
            raise KucoinAPIError(
                f"KuCoin account type mismatch: expected UNIFIED, got {account_type}"
            )

        equities = {}
        for account in data.get("accounts") or []:
            for item in account.get("currencies") or []:
                currency = str(item.get("currency") or "").strip().upper()
                if not currency:
                    raise KucoinAPIError("KuCoin returned an empty currency")
                amount = float(item["equity"])
                if not math.isfinite(amount):
                    raise KucoinAPIError(
                        f"KuCoin returned a non-finite {currency} equity"
                    )
                equities[currency] = equities.get(currency, 0.0) + amount
        return equities

    @staticmethod
    def _spot_pair_prices(data):
        pair_prices = {}
        for ticker in data.get("list") or []:
            symbol = str(ticker.get("symbol") or "").strip().upper()
            if "-" not in symbol:
                continue
            base, quote = symbol.rsplit("-", 1)
            try:
                price = float(ticker.get("lastPrice"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            pair_prices[(base, quote)] = price
            pair_prices[(quote, base)] = 1.0 / price
        return pair_prices

    def _market_price(self, currency, pair_prices):
        if currency == self.ccy:
            return 1.0
        direct = pair_prices.get((currency, self.ccy))
        if direct is not None:
            return direct
        for bridge in PRICE_BRIDGE_CURRENCIES:
            if bridge in {currency, self.ccy}:
                continue
            first = pair_prices.get((currency, bridge))
            second = pair_prices.get((bridge, self.ccy))
            if first is not None and second is not None:
                return first * second
        raise KucoinAPIError(
            f"KuCoin has no spot lastPrice conversion from {currency} to {self.ccy}"
        )

    async def get_actual_equity(self):
        fallback = self.get_last_actual_equity_from_csv
        try:
            balances, tickers = await asyncio.gather(
                self.fetch_account_balances(), self.fetch_spot_tickers()
            )
            equities = self._currency_equities(balances)
            pair_prices = self._spot_pair_prices(tickers)
            actual_equity = sum(
                amount * self._market_price(currency, pair_prices)
                for currency, amount in equities.items()
                if amount != 0
            )
            if not math.isfinite(actual_equity):
                raise KucoinAPIError("Calculated KuCoin equity is non-finite")
            RUNTIME_LOGGER.info(
                "[%s] KuCoin market-price equity %.8f %s (%d currencies)",
                self.name,
                actual_equity,
                self.ccy,
                len(equities),
            )
            return actual_equity
        except Exception as exc:
            RUNTIME_LOGGER.error("[%s] KuCoin actual equity fetch failed: %s", self.name, exc)
            return fallback()
