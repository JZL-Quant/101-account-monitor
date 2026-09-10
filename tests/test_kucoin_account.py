import base64
import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from core.exchange_accounts.kucoin import KucoinExchangeAccount


def build_account(snapshot_file):
    return KucoinExchangeAccount(
        name="KuCoin_Test",
        api_key="key",
        secret="secret",
        passphrase="passphrase",
        initial_unit=1000,
        account_type="account",
        ccy="USDT",
        minute_snapshot_file=str(snapshot_file),
        api_key_version="2",
    )


class KucoinExchangeAccountTests(unittest.IsolatedAsyncioTestCase):
    def test_declared_passphrase_is_normalized(self):
        self.assertEqual(
            KucoinExchangeAccount.normalize_credentials({"passphrase": " pass "}),
            {"passphrase": "pass"},
        )

    async def test_api_key_version_is_detected_from_api_key_info(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = KucoinExchangeAccount(
                name="KuCoin_Test",
                api_key="key",
                secret="secret",
                passphrase="passphrase",
                initial_unit=1000,
                account_type="account",
                minute_snapshot_file=str(Path(temp_dir) / "snapshot.csv"),
                api_key_version=None,
            )
            calls = []

            def request_sync(method, path, params=None, api_key_version=None):
                calls.append((path, api_key_version))
                return {"apiVersion": 3}

            account._request_sync = request_sync
            detected = await account.ensure_api_key_version()

        self.assertEqual(detected, "3")
        self.assertEqual(account.api_key_version, "3")
        self.assertEqual(calls, [("/api/ua/v2/user/api-key", "3")])

    async def test_api_key_version_falls_back_to_version_two(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = KucoinExchangeAccount(
                name="KuCoin_Test",
                api_key="key",
                secret="secret",
                passphrase="passphrase",
                initial_unit=1000,
                account_type="account",
                minute_snapshot_file=str(Path(temp_dir) / "snapshot.csv"),
                api_key_version=None,
            )
            calls = []

            def request_sync(method, path, params=None, api_key_version=None):
                calls.append(api_key_version)
                if api_key_version == "3":
                    raise RuntimeError("wrong version")
                return {"apiVersion": 2}

            account._request_sync = request_sync
            detected = await account.ensure_api_key_version()

        self.assertEqual(detected, "2")
        self.assertEqual(calls, ["3", "2"])

    def test_private_headers_follow_kucoin_signature_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            endpoint = "/api/ua/v2/unified/account/balance"

            headers = account._private_headers(
                "GET", endpoint, timestamp_ms=1700000000000
            )

        expected_signature = base64.b64encode(
            hmac.new(
                b"secret",
                f"1700000000000GET{endpoint}".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        expected_passphrase = base64.b64encode(
            hmac.new(b"secret", b"passphrase", hashlib.sha256).digest()
        ).decode()
        self.assertEqual(headers["KC-API-SIGN"], expected_signature)
        self.assertEqual(headers["KC-API-PASSPHRASE"], expected_passphrase)
        self.assertEqual(headers["KC-API-KEY-VERSION"], "2")

    async def test_actual_equity_uses_currency_equity_and_spot_last_price(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            account.fetch_account_balances = AsyncMock(return_value={
                "accountType": "UNIFIED",
                "accounts": [{
                    "currencies": [
                        {"currency": "USDT", "equity": "1000"},
                        {"currency": "BTC", "equity": "0.01"},
                    ]
                }],
            })
            account.fetch_spot_tickers = AsyncMock(return_value={
                "list": [
                    {"symbol": "BTC-USDT", "lastPrice": "65000"},
                ]
            })

            equity = await account.get_actual_equity()

        self.assertEqual(equity, 1650.0)

    async def test_actual_equity_can_use_a_two_leg_spot_conversion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            account.fetch_account_balances = AsyncMock(return_value={
                "accountType": "UNIFIED",
                "accounts": [{
                    "currencies": [{"currency": "TOKEN", "equity": "2"}]
                }],
            })
            account.fetch_spot_tickers = AsyncMock(return_value={
                "list": [
                    {"symbol": "TOKEN-BTC", "lastPrice": "0.001"},
                    {"symbol": "BTC-USDT", "lastPrice": "65000"},
                ]
            })

            equity = await account.get_actual_equity()

        self.assertEqual(equity, 130.0)

    async def test_missing_market_price_falls_back_instead_of_undervaluing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            account.fetch_account_balances = AsyncMock(return_value={
                "accountType": "UNIFIED",
                "accounts": [{
                    "currencies": [{"currency": "TOKEN", "equity": "2"}]
                }],
            })
            account.fetch_spot_tickers = AsyncMock(return_value={"list": []})
            account.get_last_actual_equity_from_csv = lambda: 987.65

            equity = await account.get_actual_equity()

        self.assertEqual(equity, 987.65)

    async def test_api_failure_falls_back_to_last_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_file = Path(temp_dir) / "snapshot.csv"
            snapshot_file.write_text(
                "timestamp,actual_equity,total_unit,net_value\n"
                "2026-08-03 10:00:00,987.65,1000,0.98765\n",
                encoding="utf-8",
            )
            account = build_account(snapshot_file)
            account.fetch_account_balances = AsyncMock(
                side_effect=RuntimeError("API unavailable")
            )
            account.fetch_spot_tickers = AsyncMock(return_value={"list": []})

            equity = await account.get_actual_equity()

        self.assertEqual(equity, 987.65)

    def test_passphrase_is_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "requires passphrase"):
                KucoinExchangeAccount(
                    name="KuCoin_Test",
                    api_key="key",
                    secret="secret",
                    passphrase="",
                    initial_unit=1000,
                    account_type="account",
                    minute_snapshot_file=str(Path(temp_dir) / "snapshot.csv"),
                )


if __name__ == "__main__":
    unittest.main()
