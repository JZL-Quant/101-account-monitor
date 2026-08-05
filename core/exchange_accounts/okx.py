import asyncio
import base64
import hashlib
import hmac
import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

from .base import BaseExchangeAccount, CredentialField, RUNTIME_LOGGER


OKX_API_BASE_URL = "https://www.okx.com"
ASSET_VALUATION_PATH = "/api/v5/asset/asset-valuation"


class OkxAPIError(RuntimeError):
    pass


class OkxExchangeAccount(BaseExchangeAccount):
    """OKX account adapter using the total account asset valuation endpoint."""

    exchange_id = "okx"
    exchange_label = "OKX"
    supported_account_types = ("account", "account_pro")
    credential_fields = (CredentialField.passphrase(),)

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
            exchange=account_info.get("exchange", "OKX"),
            minute_snapshot_file=account_info.get("minute_snapshot_file"),
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
        exchange="OKX",
        minute_snapshot_file=None,
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
            raise ValueError(f"OKX account {name} requires passphrase")

    @staticmethod
    def _utc_timestamp():
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _private_headers(self, method, endpoint, body="", timestamp=None):
        timestamp = timestamp or self._utc_timestamp()
        prehash = f"{timestamp}{method.upper()}{endpoint}{body}"
        signature = base64.b64encode(
            hmac.new(
                self.secret.encode("utf-8"),
                prehash.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def _request_sync(self, method, path, params=None, body=""):
        query = urlencode(params or {})
        endpoint = path + (f"?{query}" if query else "")
        request = urllib.request.Request(
            OKX_API_BASE_URL + endpoint,
            data=body.encode("utf-8") if body else None,
            headers=self._private_headers(method, endpoint, body),
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise OkxAPIError(f"OKX HTTP {exc.code}: {response_body[:300]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise OkxAPIError(f"OKX request failed: {exc}") from exc

        if str(payload.get("code", "")) != "0":
            message = payload.get("msg") or "unknown error"
            raise OkxAPIError(f"OKX error {payload.get('code')}: {message}")
        return payload.get("data") or []

    async def request(self, path, api=None, method="GET", params=None, body="", **_kwargs):
        return await asyncio.to_thread(self._request_sync, method, path, params, body)

    async def fetch_asset_valuation(self):
        data = await self.request(
            ASSET_VALUATION_PATH,
            method="GET",
            params={"ccy": self.ccy},
        )
        if not isinstance(data, list) or not data:
            raise OkxAPIError("OKX returned empty asset valuation data")
        return data[0]

    async def fetch_account_assets(self, account_type: str):
        return await self.fetch_asset_valuation()

    async def fetch_tickers(self):
        raise NotImplementedError("OKX total asset valuation does not require ticker data")

    async def fetch_rwusd_account(self):
        return {}

    async def validate_credentials(self):
        await self.fetch_asset_valuation()

    async def get_actual_equity(self):
        fallback = self.get_last_actual_equity_from_csv
        try:
            data = await self.fetch_asset_valuation()
            actual_equity = float(data["totalBal"])
            if not math.isfinite(actual_equity):
                raise OkxAPIError("OKX returned a non-finite totalBal")
            RUNTIME_LOGGER.info(
                "[%s] OKX equity %.8f %s", self.name, actual_equity, self.ccy
            )
            return actual_equity
        except Exception as exc:
            RUNTIME_LOGGER.error("[%s] OKX actual equity fetch failed: %s", self.name, exc)
            return fallback()
