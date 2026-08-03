import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from core.feishu.daily_report_card import build_daily_detail_cards


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


if __name__ == "__main__":
    unittest.main()
