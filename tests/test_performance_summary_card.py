import unittest
from datetime import date

from core.feishu.performance_summary_card import build_return_performance_card


class PerformanceSummaryCardTests(unittest.TestCase):
    def test_card_has_only_24h_and_7d_tables_with_expected_columns(self):
        sections = [
            {
                "period": "24h",
                "title": "24h 收益率表现",
                "rows": [{
                    "level": "priority_attention",
                    "account_name": "Example",
                    "return_value": -2.0,
                    "group_mean": 1.0,
                    "group_std": 1.5,
                    "z_value": -2.0,
                    "group_label": "Binance-USDT",
                }],
            },
            {"period": "7D", "title": "7D 收益率表现", "rows": []},
        ]

        card = build_return_performance_card(sections, date(2026, 8, 3))
        tables = [element for element in card["body"]["elements"] if element["tag"] == "table"]

        self.assertEqual(card["header"]["title"]["content"], "收益率账户表现总览")
        self.assertEqual(card["header"]["subtitle"]["content"], "2026-08-03")
        self.assertEqual(len(tables), 2)
        self.assertEqual(
            [column["display_name"] for column in tables[0]["columns"]],
            ["账户", "收益率", "同组均值", "标准差", "Z 值", "交易所-本位"],
        )
        self.assertEqual(tables[0]["rows"][0]["account"], "🟠 Example")
        self.assertNotIn("30D", str(card))

    def test_new_account_name_has_marker(self):
        sections = [{
            "period": "24h",
            "title": "24h 收益率表现",
            "rows": [{
                "level": "attention",
                "account_name": "Test_1",
                "return_value": -1.0,
                "group_mean": 0.0,
                "group_std": 0.5,
                "z_value": -2.0,
                "group_label": "Binance-USDT",
                "is_new_account": True,
            }],
        }]

        card = build_return_performance_card(sections, date(2026, 8, 3))
        table = next(element for element in card["body"]["elements"] if element["tag"] == "table")

        self.assertEqual(table["rows"][0]["account"], "🟡 Test_1 🌱🆕")


if __name__ == "__main__":
    unittest.main()
