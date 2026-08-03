import unittest
from datetime import date

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


if __name__ == "__main__":
    unittest.main()
