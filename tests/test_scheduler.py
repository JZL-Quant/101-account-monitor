import asyncio
import unittest
from datetime import datetime

from core.scheduler import MonitorScheduler


class StopAfterStartupScheduler(MonitorScheduler):
    async def _sleep_until_next_minute(self):
        raise asyncio.CancelledError


class SchedulerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_task_runs_without_daily_task(self):
        calls = []
        scheduler = StopAfterStartupScheduler()

        async def startup_task():
            calls.append("startup")

        async def daily_task():
            calls.append("daily")

        scheduler.add_task("startup", startup_task)
        scheduler.add_task("daily", daily_task)

        with self.assertRaises(asyncio.CancelledError):
            await scheduler._run()

        self.assertEqual(calls, ["startup"])

    async def test_weekly_due_only_once_in_configured_week(self):
        scheduler = MonitorScheduler()
        scheduler._weekly_weekday = 0
        scheduler._weekly_hour = 10
        scheduler._weekly_minute = 35
        monday = datetime(2026, 8, 10, 10, 35)
        self.assertTrue(scheduler._weekly_due(monday))
        scheduler._last_weekly_run_key = (2026, 33)
        self.assertFalse(scheduler._weekly_due(monday))


if __name__ == "__main__":
    unittest.main()
