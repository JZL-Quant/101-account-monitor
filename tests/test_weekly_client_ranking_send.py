import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from core.weekly_client_ranking import send_weekly_client_ranking


class MetricsStore:
    def read(self, account_name, metric_name):
        return {
            "report_actual_equity": 10000,
            "annualized_return_7d": 12.5,
        }[metric_name]


class Notifier:
    def __init__(self):
        self.send_card = AsyncMock()


class WeeklyClientRankingSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_route_uses_shared_sender_without_persisting_history(self):
        refresh = AsyncMock()
        notifier = Notifier()
        accounts = {"A": {"client": "Alice", "ccy": "USDT"}}
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.json"
            ranking = await send_weekly_client_ranking(
                get_account_map=lambda: accounts,
                metrics_store=MetricsStore(),
                notifier=notifier,
                refresh_metrics=refresh,
                history_path=history_path,
                route="test",
                require_route=True,
                persist_history=False,
            )
            self.assertFalse(history_path.exists())

        refresh.assert_awaited_once_with()
        notifier.send_card.assert_awaited_once()
        self.assertEqual(ranking.groups[0]["rows"][0]["client"], "Alice")


if __name__ == "__main__":
    unittest.main()
