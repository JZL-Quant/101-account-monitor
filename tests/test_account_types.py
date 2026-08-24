import unittest

from nav_service import state


class AccountTypeTests(unittest.TestCase):
    def test_classic_is_only_creatable_for_binance(self):
        self.assertEqual(state.normalize_account_type("classic", "binance"), "classic")
        with self.assertRaisesRegex(ValueError, "普通账户.*Pro 账户"):
            state.normalize_account_type("classic", "gate")

    def test_exchange_options_expose_classic_for_binance_only(self):
        options = {item["id"]: item for item in state.build_exchange_options()}
        binance_types = {
            item["value"]: item["label"]
            for item in options["binance"]["account_types"]
        }
        self.assertEqual(binance_types["classic"], "CLASSIC 账户")
        for exchange_id in ("gate", "kucoin", "okx"):
            self.assertNotIn(
                "classic",
                {
                    item["value"]
                    for item in options[exchange_id]["account_types"]
                },
            )


if __name__ == "__main__":
    unittest.main()
