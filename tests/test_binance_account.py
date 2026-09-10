import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from core.exchange_accounts.binance import (
    BinanceExchangeAccount,
    _BinanceRateLimitController,
)


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
    async def test_all_account_types_count_spot_and_margin_locked_once(self):
        for account_type in ("classic", "account", "account_pro"):
            with self.subTest(account_type=account_type), tempfile.TemporaryDirectory() as temp_dir:
                account = build_account(Path(temp_dir) / "snapshot.csv", account_type)
                try:
                    account.fetch_tickers = AsyncMock(return_value=[
                        {"symbol": "BTCUSDT", "price": "50000"},
                    ])
                    account.client.privateGetAccount = AsyncMock(return_value={
                        "balances": [
                            {"asset": "BTC", "free": "0.10", "locked": "0.02"},
                            {"asset": "USDT", "free": "0", "locked": "30"},
                            {"asset": "USDT", "free": "7"},
                        ],
                    })
                    account.client.sapi_get_margin_account = AsyncMock(return_value={
                        "userAssets": [{
                            "asset": "USDT", "free": "100", "locked": "20",
                            "borrowed": "10", "interest": "1",
                        }],
                    })
                    margin = [{
                        "asset": "USDT", "crossMarginAsset": "120",
                        "crossMarginBorrowed": "10", "crossMarginInterest": "1",
                        "crossMarginLocked": "20",
                    }]
                    account.client.papi_get_balance = AsyncMock(return_value=margin)
                    account.client.sapi_get_portfolio_balance = AsyncMock(return_value=margin)
                    account.client.fapiPrivateV3GetAccount = AsyncMock(return_value={
                        "assets": [{"asset": "USDT", "walletBalance": "50", "unrealizedProfit": "5"}],
                    })
                    account.client.papi_get_um_account = AsyncMock(return_value={
                        "assets": [{"asset": "USDT", "crossWalletBalance": "50", "crossUnPnl": "5"}],
                    })
                    account.fetch_rwusd_account = AsyncMock(return_value={"rwusdAmount": "0"})

                    equity = await account.get_actual_equity()

                    # Spot 6037 + margin 109 + futures 55; each locked balance counts once.
                    self.assertAlmostEqual(equity, 6201.0)
                finally:
                    await account.close()

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
        self.assertEqual(assets["spotaccount_balances"][0]["free"], "0.10")
        self.assertEqual(assets["spotaccount_balances"][0]["locked"], "0.02")

    async def test_classic_equity_includes_current_rwusd_but_not_total_profit(self):
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
                "spotaccount_balances": [{"asset": "BTC", "free": "0.10", "locked": "0.02"}],
                "futures_data": {"assets": [{
                    "asset": "USDT",
                    "walletBalance": "100",
                    "unrealizedProfit": "5",
                }]},
                "rw_data": {"rwusdAmount": "10", "totalProfit": "2"},
            })

            equity = await account.get_actual_equity()
            await account.close()

        self.assertEqual(equity, 19115.0)

    async def test_rate_limit_waits_until_binance_unban_time_then_retries(self):
        now = [1_787_738_500.0]
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        controller = _BinanceRateLimitController(
            clock=lambda: now[0],
            sleep=fake_sleep,
        )
        responses = iter([
            Exception(
                'binance 418 I\'m a teapot {"code":-1003,'
                '"msg":"Way too many requests; IP banned until 1787738510124."}'
            ),
            {"ok": True},
        ])

        async def request():
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        result = await controller.call(
            request,
            account_name="Binance_Test",
            operation_name="test_request",
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 11.124, places=3)

    async def test_rate_limit_uses_retry_after_when_unban_time_is_missing(self):
        now = [1000.0]
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        response = type("Response", (), {"status_code": 429, "headers": {"Retry-After": "7"}})()
        error = Exception("request failed")
        error.response = response
        controller = _BinanceRateLimitController(clock=lambda: now[0], sleep=fake_sleep)
        attempts = 0

        async def request():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise error
            return "done"

        result = await controller.call(
            request,
            account_name="Binance_Test",
            operation_name="test_request",
        )

        self.assertEqual(result, "done")
        self.assertEqual(sleeps, [7.0])

    async def test_rate_limit_allows_normal_requests_to_run_concurrently(self):
        controller = _BinanceRateLimitController()
        both_started = asyncio.Event()
        started = 0

        async def request(result):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.5)
            return result

        results = await asyncio.gather(
            controller.call(
                lambda: request("first"),
                account_name="Binance_First",
                operation_name="test_request",
            ),
            controller.call(
                lambda: request("second"),
                account_name="Binance_Second",
                operation_name="test_request",
            ),
        )

        self.assertEqual(results, ["first", "second"])
        self.assertEqual(started, 2)

    async def test_rate_limit_keeps_cooldown_shared_for_later_accounts(self):
        now = [1000.0]
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        controller = _BinanceRateLimitController(clock=lambda: now[0], sleep=fake_sleep)
        controller._extend_cooldown(7.0)

        result = await controller.call(
            AsyncMock(return_value="done"),
            account_name="Binance_Other",
            operation_name="test_request",
        )

        self.assertEqual(result, "done")
        self.assertEqual(sleeps, [7.0])

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
