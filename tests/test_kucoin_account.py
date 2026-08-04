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
    def test_private_headers_follow_kucoin_signature_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            endpoint = "/api/ua/v1/asset/valuation?base=USDT"

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

    async def test_actual_equity_uses_total_valuation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            account.fetch_asset_valuation = AsyncMock(return_value={
                "currentAccount": {
                    "base": "USDT",
                    "totalValuation": "1234.5678",
                }
            })

            equity = await account.get_actual_equity()

        self.assertEqual(equity, 1234.5678)

    async def test_api_failure_falls_back_to_last_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_file = Path(temp_dir) / "snapshot.csv"
            snapshot_file.write_text(
                "timestamp,actual_equity,total_unit,net_value\n"
                "2026-08-03 10:00:00,987.65,1000,0.98765\n",
                encoding="utf-8",
            )
            account = build_account(snapshot_file)
            account.fetch_asset_valuation = AsyncMock(
                side_effect=RuntimeError("API unavailable")
            )

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
