import unittest
import json
import tempfile
from pathlib import Path
from datetime import date

from core.feishu.weekly_client_ranking_card import build_weekly_client_ranking_card
from core.weekly_client_ranking import (
    apply_previous_ranks,
    build_weekly_client_ranking,
    load_previous_ranks,
    save_ranking_snapshot,
)


class MetricsStore:
    def __init__(self, values):
        self.values = values

    def read(self, account_name, metric_name):
        return self.values.get((account_name, metric_name), float("nan"))


class WeeklyClientRankingTests(unittest.TestCase):
    def test_aggregates_accounts_by_client_with_equity_weighting(self):
        accounts = {
            "A1": {"client": "Alice", "exchange_id": "binance", "exchange_label": "Binance", "ccy": "USDT"},
            "A2": {"client": "Alice", "exchange_id": "binance", "exchange_label": "Binance", "ccy": "USDT"},
            "B1": {"client": "Bob", "exchange_id": "binance", "exchange_label": "Binance", "ccy": "USDT"},
        }
        values = {
            ("A1", "report_actual_equity"): 100,
            ("A1", "annualized_return_7d"): 10,
            ("A2", "report_actual_equity"): 300,
            ("A2", "annualized_return_7d"): 30,
            ("B1", "report_actual_equity"): 200,
            ("B1", "annualized_return_7d"): 20,
        }
        ranking = build_weekly_client_ranking(
            accounts, MetricsStore(values), date(2026, 8, 10), {"USDT": 1.0}
        )

        self.assertEqual([row["client"] for row in ranking.groups[0]["rows"]], ["Alice", "Bob"])
        self.assertEqual(ranking.groups[0]["rows"][0]["annualized_return_7d"], 25.0)
        self.assertEqual(ranking.groups[0]["rows"][0]["account_count"], 2)

        card = build_weekly_client_ranking_card(ranking)
        table = next(item for item in card["body"]["elements"] if item["tag"] == "table")
        self.assertEqual(table["rows"][0]["rank"], "🥇")
        self.assertEqual(table["rows"][0]["accounts"], "2/2")

    def test_equity_column_converts_b_and_u_to_usdt(self):
        accounts = {
            "B": {"client": "Alice", "ccy": "BTC"},
            "U": {"client": "Alice", "ccy": "USDT"},
        }
        values = {
            ("B", "report_actual_equity"): 10,
            ("B", "annualized_return_7d"): 20,
            ("U", "report_actual_equity"): 1000,
            ("U", "annualized_return_7d"): 10,
        }
        ranking = build_weekly_client_ranking(
            accounts, MetricsStore(values), equity_to_usdt={"BTC": 100000}
        )
        card = build_weekly_client_ranking_card(ranking)
        table = next(item for item in card["body"]["elements"] if item["tag"] == "table")
        self.assertEqual(table["rows"][0]["equity"], "100")
        self.assertEqual(table["columns"][2]["display_name"], "7D加权年化")
        self.assertEqual(table["columns"][3]["display_name"], "有效权益(wU)")
        self.assertEqual(
            [column["display_name"] for column in table["columns"]],
            ["排名", "客户", "7D加权年化", "有效权益(wU)", "较上周", "有效账户"],
        )

    def test_rank_change_and_history_round_trip(self):
        accounts = {
            "A": {"client": "Alice", "ccy": "USDT"},
            "B": {"client": "Bob", "ccy": "USDT"},
        }
        values = {
            ("A", "report_actual_equity"): 100,
            ("A", "annualized_return_7d"): 20,
            ("B", "report_actual_equity"): 100,
            ("B", "annualized_return_7d"): 10,
        }
        ranking = build_weekly_client_ranking(
            accounts, MetricsStore(values), date(2026, 8, 10)
        )
        apply_previous_ranks(ranking, {"Alice": 2, "Bob": 1})
        self.assertEqual(ranking.groups[0]["rows"][0]["rank_change"], 1)
        self.assertEqual(ranking.groups[0]["rows"][1]["rank_change"], -1)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            save_ranking_snapshot(path, ranking)
            payload = json.loads(path.read_text())
            self.assertEqual(payload["snapshots"][0]["ranks"], {"Alice": 1, "Bob": 2})
            self.assertEqual(load_previous_ranks(path, date(2026, 8, 17)), {"Alice": 1, "Bob": 2})

    def test_invalid_account_is_visible_as_partial_coverage(self):
        accounts = {
            "A1": {"client": "Alice", "exchange_id": "gate", "exchange": "Gate", "ccy": "BTC"},
            "A2": {"client": "Alice", "exchange_id": "gate", "exchange": "Gate", "ccy": "BTC"},
        }
        values = {
            ("A1", "report_actual_equity"): 2,
            ("A1", "annualized_return_7d"): 15,
        }
        ranking = build_weekly_client_ranking(
            accounts, MetricsStore(values), equity_to_usdt={"BTC": 100000}
        )
        row = ranking.groups[0]["rows"][0]
        self.assertEqual((row["valid_account_count"], row["account_count"]), (1, 2))
        self.assertEqual(len(ranking.skipped_accounts), 1)


if __name__ == "__main__":
    unittest.main()
