import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from core.feishu.daily_report_card import build_daily_detail_cards, build_group_schema2_card


class DailyReportCardTests(unittest.TestCase):
    def test_builds_one_card_for_each_detail_group(self):
        report = SimpleNamespace(
            report_date=date(2026, 8, 3),
            detail_groups=[
                {"group": "g1", "results": "r1", "combined": "c1"},
                {"group": "g2", "results": "r2", "combined": "c2"},
            ],
        )

        with patch(
            "core.feishu.daily_report_card.build_group_schema2_card",
            side_effect=["card-1", "card-2"],
        ) as card_builder:
            cards = build_daily_detail_cards(report)

        self.assertEqual(cards, ["card-1", "card-2"])
        self.assertEqual(card_builder.call_count, 2)
        self.assertEqual(card_builder.call_args_list[0].args, ("g1", "r1", "2026-08-03", "c1"))

    def test_new_account_name_has_marker_without_extra_color(self):
        card = build_group_schema2_card(
            {"title": "Binance_Test_USDT", "benchmark": 4.0, "ccy": "USDT"},
            [{
                "display_name": "Test_1",
                "actual_equity": 100,
                "ar24h": 1,
                "ar7d": 2,
                "ar30d": 3,
                "is_new_account": True,
            }],
            "2026-08-03",
            {"ar24h": 1, "ar7d": 2, "ar30d": 3},
        )
        table = next(
            element for element in card["body"]["elements"]
            if element["tag"] == "table"
        )

        self.assertEqual(table["rows"][0]["account_name"], "Test_1 🌱🆕")


if __name__ == "__main__":
    unittest.main()
