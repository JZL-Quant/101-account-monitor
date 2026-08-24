import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from core.exchange_accounts.binance import BinanceExchangeAccount


def build_account(snapshot_file, account_type="classic"):
    return BinanceExchangeAccount(
        name="Binance_Test",
        api_key="key",
        secret="secret",
        initial_unit=1000,
        account_type=account_type,
        ccy="USDT",
        minute_snapshot_file=str(snapshot_file),
    )


class BinanceClassicAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_classic_assets_are_normalized_for_equity_calculation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            account.client.sapi_get_margin_account = AsyncMock(return_value={
                "accountType": "PM_2",
                "userAssets": [{
                    "asset": "BTC",
                    "free": "0.30",
                    "locked": "0.02",
                    "borrowed": "0.05",
                    "interest": "0.01",
                }],
            })
            account.client.privateGetAccount = AsyncMock(return_value={
                "balances": [{
                    "asset": "BTC",
                    "free": "0.10",
                    "locked": "0.02",
                }],
            })
            account.client.fapiPrivateV3GetAccount = AsyncMock(return_value={
                "assets": [{
                    "asset": "USDT",
                    "walletBalance": "100",
                    "unrealizedProfit": "5",
                }],
            })
            account.fetch_rwusd_account = AsyncMock(return_value={
                "rwusdAmount": "10",
                "totalProfit": "2",
            })

            assets = await account.fetch_account_assets("classic")
            await account.close()

        self.assertEqual(assets["spot_balances"], [{
            "asset": "BTC",
            "crossMarginAsset": 0.32,
            "crossMarginBorrowed": "0.05",
            "crossMarginInterest": "0.01",
        }])
        self.assertAlmostEqual(assets["spotaccount_balances"][0]["free"], 0.12)

    async def test_classic_equity_includes_margin_spot_futures_and_rwusd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            account.fetch_tickers = AsyncMock(return_value=[
                {"symbol": "BTCUSDT", "price": "50000"},
                {"symbol": "USDCUSDT", "price": "1"},
            ])
            account.fetch_account_assets = AsyncMock(return_value={
                "spot_balances": [{
                    "asset": "BTC",
                    "crossMarginAsset": 0.32,
                    "crossMarginBorrowed": "0.05",
                    "crossMarginInterest": "0.01",
                }],
                "spotaccount_balances": [{"asset": "BTC", "free": 0.12}],
                "futures_data": {"assets": [{
                    "asset": "USDT",
                    "walletBalance": "100",
                    "unrealizedProfit": "5",
                }]},
                "rw_data": {"rwusdAmount": "10", "totalProfit": "2"},
            })

            equity = await account.get_actual_equity()
            await account.close()

        self.assertEqual(equity, 19117.0)

    async def test_classic_validation_accepts_pm_2_margin_response(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            account.client.sapi_get_margin_account = AsyncMock(return_value={
                "accountType": "PM_2",
                "userAssets": [],
            })
            account.client.privateGetAccount = AsyncMock()
            account.client.fapiPrivateV3GetAccount = AsyncMock()
            account.fetch_rwusd_account = AsyncMock()

            await account.validate_credentials()

            account.client.sapi_get_margin_account.assert_awaited_once_with()
            account.client.privateGetAccount.assert_awaited_once_with()
            account.client.fapiPrivateV3GetAccount.assert_awaited_once_with()
            account.fetch_rwusd_account.assert_awaited_once_with()
            await account.close()


if __name__ == "__main__":
    unittest.main()
