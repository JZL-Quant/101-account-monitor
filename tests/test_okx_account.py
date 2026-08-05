import base64
import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from core.exchange_accounts.okx import OkxExchangeAccount


def build_account(snapshot_file):
    return OkxExchangeAccount(
        name="OKX_Test",
        api_key="key",
        secret="secret",
        passphrase="passphrase",
        initial_unit=1000,
        account_type="account",
        ccy="USDT",
        minute_snapshot_file=str(snapshot_file),
    )


class OkxExchangeAccountTests(unittest.IsolatedAsyncioTestCase):
    def test_declared_credentials_are_normalized(self):
        self.assertEqual(
            OkxExchangeAccount.normalize_credentials({"passphrase": "  pass  "}),
            {"passphrase": "pass"},
        )
        with self.assertRaisesRegex(ValueError, "OKX API Passphrase"):
            OkxExchangeAccount.normalize_credentials({})

    def test_private_headers_follow_okx_signature_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            endpoint = "/api/v5/asset/asset-valuation?ccy=USDT"
            timestamp = "2026-08-05T01:02:03.456Z"

            headers = account._private_headers("GET", endpoint, timestamp=timestamp)

        expected_signature = base64.b64encode(
            hmac.new(
                b"secret",
                f"{timestamp}GET{endpoint}".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        self.assertEqual(headers["OK-ACCESS-SIGN"], expected_signature)
        self.assertEqual(headers["OK-ACCESS-TIMESTAMP"], timestamp)
        self.assertEqual(headers["OK-ACCESS-PASSPHRASE"], "passphrase")

    async def test_actual_equity_uses_total_balance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = build_account(Path(temp_dir) / "snapshot.csv")
            account.fetch_asset_valuation = AsyncMock(return_value={
                "totalBal": "1234.5678",
                "details": {"funding": "34.5", "trading": "1200.0678"},
            })

            equity = await account.get_actual_equity()

        self.assertEqual(equity, 1234.5678)

    async def test_api_failure_falls_back_to_last_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_file = Path(temp_dir) / "snapshot.csv"
            snapshot_file.write_text(
                "timestamp,actual_equity,total_unit,net_value\n"
                "2026-08-05 10:00:00,987.65,1000,0.98765\n",
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
                OkxExchangeAccount(
                    name="OKX_Test",
                    api_key="key",
                    secret="secret",
                    passphrase="",
                    initial_unit=1000,
                    account_type="account",
                    minute_snapshot_file=str(Path(temp_dir) / "snapshot.csv"),
                )


if __name__ == "__main__":
    unittest.main()
