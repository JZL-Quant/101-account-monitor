from datetime import datetime

try:
    import gate_api
    from gate_api import FuturesApi, MarginApi, SpotApi, WalletApi
except ImportError:  # pragma: no cover - handled at runtime with a clear error.
    gate_api = None
    FuturesApi = MarginApi = SpotApi = WalletApi = None

from .base import BaseExchangeAccount, RUNTIME_LOGGER
from ..feishu import NOTIFIER


class PriceMissingError(Exception):
    def __init__(self, currency, amount):
        super().__init__(f"{currency} has position {amount:.8f}, but no valid USDT price")
        self.currency = currency
        self.amount = amount


class GateExchangeAccount(BaseExchangeAccount):
    supported_account_types = ("account", "account_pro")

    @classmethod
    def from_account_info(cls, name: str, account_info: dict):
        return cls(
            name,
            account_info["key"],
            account_info["secret"],
            account_info["initial_unit"],
            account_info["account_type"],
            account_info.get("ccy", "USDT"),
            account_info.get("exchange", "Gate"),
            account_info.get("minute_snapshot_file"),
            account_info.get("blacklist", ()),
        )

    def __init__(
        self,
        name,
        api_key,
        secret,
        initial_unit,
        account_type,
        ccy="USDT",
        exchange="Gate",
        minute_snapshot_file=None,
        blacklist=(),
    ):
        super().__init__(name, api_key, secret, initial_unit, account_type, ccy, exchange, minute_snapshot_file)
        if gate_api is None:
            raise RuntimeError("gate_api is required for Gate accounts. Install gate-api before enabling Gate.")

        self.blacklist = set(blacklist or ())
        configuration = gate_api.Configuration(
            host="https://api.gateio.ws/api/v4",
            key=api_key,
            secret=secret,
        )
        self.api_client = gate_api.ApiClient(configuration)
        self.spot_api = SpotApi(self.api_client)
        self.margin_api = MarginApi(self.api_client)
        self.futures_api = FuturesApi(self.api_client)
        self.wallet_api = WalletApi(self.api_client)

    async def request(self, *args, **kwargs):
        raise NotImplementedError("Gate uses the official gate_api SDK instead of BaseExchangeAccount.request")

    async def fetch_tickers(self):
        return self.spot_api.list_tickers()

    async def fetch_account_assets(self, account_type: str):
        return {
            "spot_accounts": self.spot_api.list_spot_accounts(),
            "margin_accounts": self.margin_api.list_margin_accounts(),
            "futures_account": self.futures_api.list_futures_accounts("usdt"),
        }

    async def fetch_rwusd_account(self):
        return {}

    async def get_price_map(self):
        price_map = {"USDT": 1.0}
        try:
            for ticker in await self.fetch_tickers():
                currency_pair = getattr(ticker, "currency_pair", "")
                if not currency_pair.endswith("_USDT"):
                    continue
                currency = currency_pair.replace("_USDT", "")
                price_map[currency] = float(ticker.last)
        except Exception as exc:
            RUNTIME_LOGGER.error("[%s] Gate price fetch failed: %s", self.name, exc)
        return price_map

    def get_price_or_raise(self, currency, price_map, amount):
        price = price_map.get(currency, 0.0)
        if price:
            return price
        if currency in self.blacklist:
            return 0.0
        raise PriceMissingError(currency, amount)

    async def handle_price_missing(self, exc: PriceMissingError, missing_currencies: set, source: str):
        currency = exc.currency
        if currency in missing_currencies:
            return
        missing_currencies.add(currency)
        msg = (
            f"[{self.name}] Gate price missing\n"
            f"- time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- account: {self.name}\n"
            f"- currency: {currency}\n"
            f"- source: {source}\n"
            f"- position: {exc.amount:.8f}\n"
            "- action: skipped from this equity snapshot"
        )
        RUNTIME_LOGGER.warning(msg)
        try:
            await NOTIFIER.send_message(msg)
        except Exception as notify_exc:
            RUNTIME_LOGGER.error("[%s] Gate missing-price notification failed: %s", self.name, notify_exc)

    async def get_spot_value(self, price_map, missing_currencies: set):
        total = 0.0
        for account in self.spot_api.list_spot_accounts():
            currency = account.currency
            amount = float(account.available) + float(account.locked)
            if amount == 0:
                continue
            try:
                price = self.get_price_or_raise(currency, price_map, amount)
            except PriceMissingError as exc:
                await self.handle_price_missing(exc, missing_currencies, "Spot")
                continue
            total += amount * price

        try:
            wallet_response = self.wallet_api.get_total_balance(currency="USDT")
            spot_borrowed = float(wallet_response.details["spot"].borrowed)
        except Exception as exc:
            RUNTIME_LOGGER.warning("[%s] Gate spot borrowed fetch failed: %s", self.name, exc)
            spot_borrowed = 0.0
        return total - spot_borrowed

    async def get_margin_value(self, price_map, missing_currencies: set):
        total = 0.0
        for account in self.margin_api.list_margin_accounts():
            for asset in (account.base, account.quote):
                currency = asset.currency
                net_amount = float(asset.available) - float(asset.borrowed)
                if net_amount == 0:
                    continue
                try:
                    price = self.get_price_or_raise(currency, price_map, net_amount)
                except PriceMissingError as exc:
                    await self.handle_price_missing(exc, missing_currencies, "Margin")
                    continue
                total += net_amount * price
        return total

    async def get_futures_value(self):
        account = self.futures_api.list_futures_accounts("usdt")
        return float(account.total) + float(account.unrealised_pnl)

    async def get_actual_equity(self):
        fallback = self.get_last_actual_equity_from_csv
        missing_currencies = set()
        try:
            price_map = await self.get_price_map()
            spot = await self.get_spot_value(price_map, missing_currencies)
            margin = await self.get_margin_value(price_map, missing_currencies)
            futures = await self.get_futures_value()
            actual_equity = spot + margin + futures
            RUNTIME_LOGGER.info(
                "[%s] Gate equity %.8f USDT (spot %.8f, margin %.8f, futures %.8f)",
                self.name,
                actual_equity,
                spot,
                margin,
                futures,
            )

            if self.ccy == "BTC":
                btc_price = price_map.get("BTC", 0.0)
                if btc_price <= 0:
                    RUNTIME_LOGGER.warning("[%s] Gate BTC price missing, fallback CSV", self.name)
                    return fallback()
                return actual_equity / btc_price
            return actual_equity
        except Exception as exc:
            RUNTIME_LOGGER.error("[%s] Gate actual equity fetch failed: %s", self.name, exc)
            return fallback()
