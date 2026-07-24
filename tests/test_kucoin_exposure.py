import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from kucoin_exposure.auth import SessionSigner, credentials_match
from kucoin_exposure.calculator import (
    build_hedge_rows,
    check_position_conversion,
    floor_to_increment,
)
from kucoin_exposure.models import FuturesPosition, SpotBalance, SpotSymbol
from kucoin_exposure.repository import ExposureRepository


class CalculatorTests(unittest.TestCase):
    def test_spot_and_short_futures_are_nettted_by_base_quantity(self):
        balances = [
            SpotBalance(
                currency="BTC",
                account_type="trade",
                balance=Decimal("1"),
                available=Decimal("0.9"),
                holds=Decimal("0.1"),
            )
        ]
        positions = [
            FuturesPosition(
                symbol="XBTUSDTM",
                base_currency="XBT",
                settle_currency="USDT",
                side="short",
                current_qty=Decimal("-980"),
                multiplier=Decimal("0.001"),
                base_qty=Decimal("-0.980"),
                mark_price=Decimal("70000"),
                mark_value=Decimal("68600"),
                avg_entry_price=Decimal("71000"),
                liquidation_price=Decimal("100000"),
                unrealised_pnl=Decimal("100"),
                leverage=Decimal("2"),
                margin_mode="CROSS",
                position_side="BOTH",
                is_inverse=False,
            )
        ]
        symbols = {
            "BTC-USDT": SpotSymbol(
                symbol="BTC-USDT",
                base_currency="BTC",
                quote_currency="USDT",
                base_increment=Decimal("0.00000001"),
                base_min_size=Decimal("0.00001"),
                min_funds=Decimal("0.1"),
                enabled=True,
            )
        }
        rows = build_hedge_rows(
            balances,
            positions,
            symbols,
            {"BTC": Decimal("70000")},
            aliases={"XBT": "BTC"},
            quote_currency="USDT",
            matched_threshold_percent=1,
            warning_threshold_percent=5,
            dust_value_usdt=1,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].spot_qty, Decimal("1"))
        self.assertEqual(rows[0].futures_qty, Decimal("-0.980"))
        self.assertEqual(rows[0].net_qty, Decimal("0.020"))
        self.assertEqual(rows[0].mismatch_percent, Decimal("2.00"))
        self.assertEqual(rows[0].status, "轻微偏多")

    def test_floor_to_increment_never_rounds_up(self):
        self.assertEqual(
            floor_to_increment(Decimal("1.23459"), Decimal("0.001")),
            Decimal("1.234"),
        )

    def test_uta_contract_conversion_matches_position_value(self):
        calculated, error, ok = check_position_conversion(
            Decimal("3"),
            Decimal("0.001"),
            Decimal("73305.4"),
            Decimal("219.9162"),
        )
        self.assertEqual(calculated, Decimal("219.9162"))
        self.assertEqual(error, Decimal("0"))
        self.assertTrue(ok)

    def test_conversion_failure_is_detected(self):
        _calculated, error, ok = check_position_conversion(
            Decimal("-1000"),
            Decimal("0.01"),
            Decimal("70000"),
            Decimal("70000"),
        )
        self.assertEqual(error, Decimal("900"))
        self.assertFalse(ok)

    def test_dust_exposure_has_zero_displayed_mismatch(self):
        balances = [
            SpotBalance(
                currency="BTC",
                account_type="unified",
                balance=Decimal("0.00000015"),
                available=Decimal("0.00000015"),
                holds=Decimal("0"),
            )
        ]
        rows = build_hedge_rows(
            balances,
            [],
            {},
            {"BTC": Decimal("66000")},
            aliases={"XBT": "BTC"},
            quote_currency="USDT",
            matched_threshold_percent=1,
            warning_threshold_percent=5,
            dust_value_usdt=1,
        )
        self.assertEqual(rows[0].net_value, Decimal("0.00990000"))
        self.assertEqual(rows[0].mismatch_percent, Decimal("0"))
        self.assertEqual(rows[0].status, "已对冲")


class AuthTests(unittest.TestCase):
    def test_signed_session_survives_until_expiry(self):
        signer = SessionSigner("a-fixed-secret-with-enough-length", 30)
        token = signer.issue("admin", now=100)
        session = signer.verify(token, now=101)
        self.assertIsNotNone(session)
        self.assertEqual(session.username, "admin")
        self.assertIsNone(signer.verify(token, now=100 + signer.max_age))

    def test_credentials(self):
        self.assertTrue(credentials_match("a", "b", "a", "b"))
        self.assertFalse(credentials_match("a", "x", "a", "b"))


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_trade_action_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ExposureRepository(Path(directory) / "test.sqlite3")
            await repository.initialize()
            action_id = await repository.save_trade_action(
                username="admin",
                asset="BTC",
                status="complete",
                request={"asset": "BTC"},
                result={"orders": []},
            )
            rows = await repository.recent_trade_actions()
            self.assertEqual(rows[0]["id"], action_id)
            self.assertEqual(rows[0]["asset"], "BTC")


if __name__ == "__main__":
    unittest.main()
