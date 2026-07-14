import ccxt.async_support as ccxt
import hashlib
import hmac
import time

import requests

from .base import BaseExchangeAccount, RUNTIME_LOGGER


class BinanceExchangeAccount(BaseExchangeAccount):
    supported_account_types = ("account_pro", "account", "account_LTP")

    def __init__(
        self,
        name,
        api_key,
        secret,
        initial_unit,
        account_type,
        ccy="USDT",
        exchange="Binance",
        minute_snapshot_file=None,
    ):
        super().__init__(name, api_key, secret, initial_unit, account_type, ccy, exchange, minute_snapshot_file)
        self.client = ccxt.binance({
            "apiKey": api_key,
            "secret": secret,
            "options": {"adjustForTimeDifference": True},
        })

    async def request(self, path, api, method, params=None, headers=None, body=None, config=None):
        return await self.client.request(
            path,
            api,
            method,
            params or {},
            headers,
            body,
            config,
        )

    async def fetch_tickers(self):
        return await self.client.public_get_ticker_price()

    async def fetch_wbeth_rate_history(self):
        return await self.client.sapiGetEthStakingEthHistoryRateHistory()

    async def fetch_account_assets(self, account_type: str):
        if account_type == "account_pro":
            spot_balances = await self.client.sapi_get_portfolio_balance()
            account_data = await self.client.privateGetAccount()
            futures_data = await self.client.fapiPrivateV3GetAccount()
            rw_data = await self.fetch_rwusd_account()
            return {
                "spot_balances": spot_balances,
                "spotaccount_balances": account_data["balances"],
                "futures_data": futures_data,
                "rw_data": rw_data,
            }

        if account_type == "account":
            spot_balances = await self.client.papi_get_balance()
            futures_data = await self.client.papi_get_um_account()
            rw_data = await self.fetch_rwusd_account()
            return {
                "spot_balances": spot_balances,
                "spotaccount_balances": [],
                "futures_data": futures_data,
                "rw_data": rw_data,
            }

        raise ValueError(f"Unsupported Binance account_type: {account_type}")

    async def fetch_rwusd_account(self):
        return await self.request(
            "rwusd/account",
            "sapi",
            "GET",
            {},
            None,
            None,
            {"sign": True},
        )

    async def validate_credentials(self):
        """Call every private API required by the selected Binance account type."""
        if self.account_type == "account_pro":
            await self.client.sapi_get_portfolio_balance()
            await self.client.privateGetAccount()
            await self.client.fapiPrivateV3GetAccount()
        elif self.account_type == "account":
            await self.client.papi_get_balance()
            await self.client.papi_get_um_account()
        else:
            raise ValueError(f"Unsupported Binance account_type: {self.account_type}")
        await self.fetch_rwusd_account()

    @staticmethod
    def get_timestamp():
        return int(time.time())

    @classmethod
    def get_ltp_header(cls, api_key: str, secret: str, params: dict = None):
        if params is None:
            params = {}

        nonce = cls.get_timestamp()
        message = "&".join([f"{arg}={params[arg]}" for arg in sorted(params.keys())]) + "&" + str(nonce)
        sign = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()

        return {
            "Content-Type": "application/json",
            "User-Agent": "PythonClient/1.0.0",
            "X-MBX-APIKEY": api_key,
            "signature": sign,
            "nonce": str(nonce),
        }

    @classmethod
    def fetch_ltp_equity(cls, api_key: str, secret: str):
        url = "https://api.liquiditytech.com/api/v1/trading/account"
        response = requests.get(url, headers=cls.get_ltp_header(api_key, secret, {}))

        if response.status_code == 200:
            RUNTIME_LOGGER.info("请求成功，返回数据：")
            RUNTIME_LOGGER.info("%s", response.json())
        else:
            RUNTIME_LOGGER.error("请求失败，状态码: %s", response.status_code)
            RUNTIME_LOGGER.error("错误信息: %s", response.text)

        equity_ltp = None
        for item in response.json()["data"]:
            if item["exchangeType"] == "BINANCE":
                equity_ltp = item["equity"]
                break
        return float(equity_ltp)

    async def convert_to_usdt(self, asset, amount, symbol_price):
        if asset == "USDT":
            return float(amount)

        if asset == "WBETH":
            try:
                wb_res = await self.fetch_wbeth_rate_history()
                rows = wb_res.get("rows")
                if not rows:
                    return 0.0

                exchange_rate = float(rows[0]["exchangeRate"])
                eth_price = symbol_price.get("ETHUSDT", 0.0)
                if eth_price <= 0:
                    return 0.0

                RUNTIME_LOGGER.info("[WBETH] Converted amount: %s", amount * exchange_rate * eth_price)
                return amount * exchange_rate * eth_price
            except Exception as exc:
                RUNTIME_LOGGER.error("[WBETH] error: %s", exc)
                return 0.0

        symbol1 = asset + "USDT"
        symbol2 = "USDT" + asset
        if symbol1 in symbol_price:
            return float(amount) * symbol_price[symbol1]
        if symbol2 in symbol_price and symbol_price[symbol2] != 0:
            return float(amount) / symbol_price[symbol2]
        return 0.0

    async def get_actual_equity(self):
        account_type = self.account_type
        ccy = self.ccy
        account_name = self.name
        fallback = self.get_last_actual_equity_from_csv
        if account_type == "account_LTP":
            try:
                actual_equity = self.fetch_ltp_equity(self.api_key, self.secret)
                if actual_equity is None:
                    RUNTIME_LOGGER.warning("[%s] LTP 未返回有效净值，回退 CSV", account_name)
                    return fallback()
                return actual_equity
            except Exception as exc:
                RUNTIME_LOGGER.error("[%s] 获取 LTP 净值异常: %s", account_name, exc)
                return fallback()

        if account_type not in ["account_pro", "account"]:
            raise ValueError(f"Unsupported Binance account_type: {account_type}")

        try:
            tickers = await self.fetch_tickers()
            if not tickers:
                RUNTIME_LOGGER.warning("[%s] tickers 为空，回退 CSV", account_name)
                return fallback()
            symbol_price = {item["symbol"]: float(item["price"]) for item in tickers}

            btc_usdt_price = symbol_price.get("BTCUSDT", 0.0)
            RUNTIME_LOGGER.info("%s", btc_usdt_price)
            if btc_usdt_price <= 0:
                RUNTIME_LOGGER.error("无法获取有效的BTC/USDT价格，无法完成转换")
                return 0.0

            try:
                account_assets = await self.fetch_account_assets(account_type)
                spot_balances = account_assets["spot_balances"]
                spotaccount_balances = account_assets["spotaccount_balances"]
                futures_data = account_assets["futures_data"]
                rw_data = account_assets["rw_data"]
            except Exception as api_exc:
                RUNTIME_LOGGER.error("API调用失败:%s-%s", account_name, api_exc)
                return fallback()

            if not spot_balances or not futures_data:
                raise ValueError("API 返回空数据，可能是接口限频或其他问题")

            spot_value, spot_detail = 0.0, []
            for balance in spot_balances:
                total_asset = float(balance["crossMarginAsset"])
                total_borrowed = float(balance["crossMarginBorrowed"])
                total_interest = float(balance["crossMarginInterest"])
                total = total_asset - total_borrowed - total_interest
                if total == 0:
                    continue
                asset = balance["asset"]
                usdt_total = await self.convert_to_usdt(asset, total, symbol_price)
                spot_value += usdt_total
                spot_detail.append({
                    "asset": asset,
                    "amount": total,
                    "usdt_total": usdt_total,
                    "coin_borrowed": total_borrowed,
                    "coin_interest": total_interest,
                })

            spotaccount_value, spotaccount_detail = 0.0, []
            for balance in spotaccount_balances:
                total = float(balance["free"])
                asset = balance["asset"]
                usdt_total = await self.convert_to_usdt(asset, total, symbol_price)
                spotaccount_value += usdt_total
                spotaccount_detail.append({"asset": asset, "amount": total, "usdt_total": usdt_total})

            futures_value, futures_detail = 0.0, []
            for balance in futures_data["assets"]:
                total = (
                    float(balance["walletBalance"]) + float(balance["unrealizedProfit"])
                    if account_type == "account_pro"
                    else float(balance["crossWalletBalance"]) + float(balance["crossUnPnl"])
                )
                if total == 0:
                    continue
                asset = balance["asset"]
                usdt_value = await self.convert_to_usdt(asset, total, symbol_price)
                futures_value += usdt_value
                futures_detail.append({"asset": asset, "amount": total, "usdt_value": usdt_value})

            rwusd_value = float(rw_data["rwusdAmount"]) + float(rw_data["totalProfit"])
            rwusdt = await self.convert_to_usdt("USDC", rwusd_value, symbol_price)

            result = {
                "spot_value": spot_value,
                "spotaccount_vlaue": spotaccount_value,
                "futures_value": futures_value,
                "rwusdt": rwusdt,
                "spot_detail": spot_detail,
                "spotaccount_detail": spotaccount_detail,
                "futures_detail": futures_detail,
            }
            RUNTIME_LOGGER.info("%s", result)

            actual_equity = spot_value + futures_value + rwusdt + spotaccount_value
            if ccy == "BTC":
                return actual_equity / btc_usdt_price
            return actual_equity

        except ValueError as exc:
            RUNTIME_LOGGER.error("获取实际资产净值时发生错误: %s", exc)
            return fallback()
        except Exception as exc:
            RUNTIME_LOGGER.error("获取实际资产净值时发生错误: %s", exc)
            if isinstance(exc, dict) and "msg" in exc and ("Way too many requests" in exc["msg"] or "418" in str(exc)):
                RUNTIME_LOGGER.warning("API 请求过于频繁，返回 CSV 数据")
                return fallback()
            return fallback()
