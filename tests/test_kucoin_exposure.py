import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

from kucoin_exposure.auth import SessionSigner, credentials_match
from kucoin_exposure.calculator import (
    build_hedge_rows,
    calculate_margin_rates,
    calculate_opening_risk_gate,
    check_position_conversion,
    floor_to_increment,
)
from kucoin_exposure.client import KucoinClient
from kucoin_exposure.config import KucoinConfig
from kucoin_exposure.models import FuturesPosition, SpotBalance, SpotSymbol
from kucoin_exposure.repository import ExposureRepository


class CalculatorTests(unittest.TestCase):
    def test_margin_rates_match_superfasttrading_safety_multiples(self):
        mmr, imr = calculate_margin_rates(
            {
                "riskRatio": "0.0085517383",
                "adjustedEquity": "49.9358942670",
                "im": "3.8681041666",
                "mm": "0.3713380000",
            }
        )
        self.assertAlmostEqual(float(mmr), 1 / 0.0085517383)
        self.assertAlmostEqual(float(imr), 49.9358942670 / 3.8681041666)

    def test_opening_risk_gate_matches_superfasttrading_thresholds(self):
        risky = calculate_opening_risk_gate(Decimal("35.21"), Decimal("1.19"))
        self.assertTrue(risky["is_risky"])
        self.assertFalse(risky["is_safe"])
        self.assertEqual(risky["result"], "禁止开仓")

        safe = calculate_opening_risk_gate(Decimal("6.45"), Decimal("1.78"))
        self.assertFalse(safe["is_risky"])
        self.assertTrue(safe["is_safe"])
        self.assertEqual(safe["result"], "允许恢复开仓")

        hysteresis = calculate_opening_risk_gate(Decimal("35.21"), Decimal("1.384"))
        self.assertFalse(hysteresis["is_risky"])
        self.assertFalse(hysteresis["is_safe"])
        self.assertEqual(hysteresis["result"], "保持当前开仓状态")

    def test_spot_and_short_futures_are_nettted_by_base_quantity(self):
        balances = [
            SpotBalance(
                currency="BTC",
                account_type="trade",
                balance=Decimal("1"),
                available=Decimal("0.9"),
                holds=Decimal("0.1"),
                equity=Decimal("1"),
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
                equity=Decimal("0.00000015"),
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

    def test_uta_liability_is_not_subtracted_twice(self):
        balances = [
            SpotBalance(
                currency="BTC",
                account_type="unified",
                balance=Decimal("-1.00"),
                available=Decimal("0"),
                holds=Decimal("0"),
                liability=Decimal("1.01"),
                equity=Decimal("-1.01"),
            )
        ]
        rows = build_hedge_rows(
            balances,
            [],
            {},
            {"BTC": Decimal("100")},
            aliases={},
            quote_currency="USDT",
            matched_threshold_percent=1,
            warning_threshold_percent=5,
            dust_value_usdt=1,
        )
        self.assertEqual(rows[0].spot_qty, Decimal("-1.01"))
        self.assertEqual(rows[0].net_value, Decimal("-101.00"))

    def test_excluded_asset_is_displayed_as_spot_reserve(self):
        balances = [
            SpotBalance(
                currency="KCS",
                account_type="unified",
                balance=Decimal("1.5"),
                available=Decimal("1.5"),
                holds=Decimal("0"),
                equity=Decimal("1.5"),
            )
        ]
        rows = build_hedge_rows(
            balances,
            [],
            {},
            {"KCS": Decimal("10")},
            aliases={"XBT": "BTC"},
            quote_currency="USDT",
            matched_threshold_percent=1,
            warning_threshold_percent=5,
            dust_value_usdt=1,
            excluded_assets=frozenset({"KCS"}),
        )
        self.assertEqual(rows[0].net_value, Decimal("15.0"))
        self.assertEqual(rows[0].mismatch_percent, Decimal("0"))
        self.assertEqual(rows[0].status, "现货储备")
        self.assertTrue(rows[0].excluded_from_hedge)


class KucoinClientV2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = KucoinClient(
            KucoinConfig("key", "secret", "passphrase")
        )
        self.client._request = AsyncMock()

    async def test_account_and_order_methods_use_uta_v2_paths(self):
        self.client._request.side_effect = [
            {"accountType": "UNIFIED", "accounts": []},
            {"accountType": "UNIFIED", "equity": "100"},
            {"items": []},
            {"items": []},
            {"orderId": "1"},
        ]

        mode = await self.client.validate_credentials()
        await self.client.fetch_futures_account("USDT")
        await self.client.cancel_spot_orders("BTC-USDT")
        await self.client.has_open_orders("BTC-USDT", "SPOT")
        await self.client.place_spot_market_sell(
            "BTC-USDT", Decimal("0.001")
        )

        self.assertEqual(mode, "UTA REST V2 / API Key V3")
        paths = [call.args[2] for call in self.client._request.await_args_list]
        self.assertEqual(
            paths,
            [
                "/api/ua/v2/unified/account/balance",
                "/api/ua/v2/unified/account/overview",
                "/api/ua/v2/unified/order/cancel-all",
                "/api/ua/v2/unified/order/open-list",
                "/api/ua/v2/unified/order/place",
            ],
        )

    async def test_v2_trading_enabled_symbol_is_available(self):
        self.client._request.return_value = {
            "list": [
                {
                    "symbol": "BTC-USDT",
                    "baseCurrency": "BTC",
                    "quoteCurrency": "USDT",
                    "baseOrderStep": "0.000001",
                    "minBaseOrderSize": "0.00001",
                    "minFunds": "0.1",
                    "tradingStatus": "TradingEnabled",
                }
            ]
        }

        symbols = await self.client.fetch_spot_symbols()

        self.assertTrue(symbols["BTC-USDT"].enabled)
        self.assertEqual(
            self.client._request.await_args.args[2],
            "/api/ua/v2/market/instrument",
        )


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
            self.assertEqual(rows[0]["username"], "admin")
            self.assertEqual(rows[0]["order_legs"], [])
            self.assertNotIn("result", rows[0])
            deleted = await repository.clear_trade_actions()
            self.assertEqual(deleted, 1)
            self.assertEqual(await repository.recent_trade_actions(), [])


if __name__ == "__main__":
    unittest.main()
