import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from core.daily_report import build_daily_report


class MetricsStore:
    def __init__(self, values):
        self.values = values

    def read(self, account_name, metric_name):
        return self.values.get((account_name, metric_name), float("nan"))


class DailyReportTests(unittest.TestCase):
    def test_builder_returns_channel_independent_report_data(self):
        accounts = {
            "A": {
                "exchange_id": "binance",
                "exchange_label": "Binance",
                "account_group": "K1",
                "ccy": "USDT",
                "interest_rate": 0.04,
            },
            "B": {
                "exchange_id": "binance",
                "exchange_label": "Binance",
                "account_group": "K1",
                "ccy": "USDT",
                "interest_rate": 0.05,
            },
        }
        values = {}
        for account_name, equity, ar24h, ar7d, ar30d in (
            ("A", 100.0, 10.0, 20.0, 30.0),
            ("B", 300.0, 14.0, 24.0, 34.0),
        ):
            values[(account_name, "report_actual_equity")] = equity
            values[(account_name, "annualized_return_24h")] = ar24h
            values[(account_name, "annualized_return_7d")] = ar7d
            values[(account_name, "annualized_return_30d")] = ar30d

        report = build_daily_report(
            accounts, MetricsStore(values), report_date=date(2026, 8, 3)
        )

        self.assertEqual(report.report_date, date(2026, 8, 3))
        self.assertEqual(len(report.detail_groups), 1)
        self.assertEqual(
            report.detail_groups[0]["combined"],
            {"ar24h": 13.0, "ar7d": 23.0, "ar30d": 33.0},
        )
        self.assertEqual(
            [section["period"] for section in report.performance_sections],
            ["24h", "7D"],
        )

    def test_marks_accounts_created_within_30_days_as_new(self):
        accounts = {
            "New": {"created_at": "2026-07-04T12:00:00+00:00"},
            "Old": {"created_at": "2026-07-03T12:00:00+00:00"},
        }

        report = build_daily_report(
            accounts, MetricsStore({}), report_date=date(2026, 8, 3)
        )
        results = {
            row["account_name"]: row["is_new_account"]
            for group in report.detail_groups
            for row in group["results"]
        }

        self.assertEqual(results, {"New": True, "Old": False})

    def test_legacy_account_uses_first_snapshot_date(self):
        with TemporaryDirectory() as temp_dir:
            snapshot_file = Path(temp_dir) / "snapshot.csv"
            snapshot_file.write_text(
                "timestamp,actual_equity\n2026-07-20 09:00:00,100\n",
                encoding="utf-8",
            )
            accounts = {"Legacy": {"minute_snapshot_file": str(snapshot_file)}}

            report = build_daily_report(
                accounts, MetricsStore({}), report_date=date(2026, 8, 3)
            )

        result = report.detail_groups[0]["results"][0]
        self.assertTrue(result["is_new_account"])


if __name__ == "__main__":
    unittest.main()
