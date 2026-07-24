import asyncio
import unittest

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


if __name__ == "__main__":
    unittest.main()
