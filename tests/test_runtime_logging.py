import logging
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from core import runtime_logging


class DailyRuntimeFileHandlerTests(unittest.TestCase):
    @staticmethod
    def _record(message="written"):
        return logging.LogRecord(
            "runtime-test",
            logging.WARNING,
            __file__,
            1,
            message,
            (),
            None,
        )

    def test_delays_file_creation_until_first_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "runtime_startup.log"
            handler = runtime_logging.DailyRuntimeFileHandler(log_path, max_bytes=0)
            try:
                self.assertFalse(log_path.exists())

                handler.emit(self._record())

                self.assertTrue(log_path.exists())
                self.assertIn("written", log_path.read_text(encoding="utf-8"))
            finally:
                handler.close()

    def test_date_switch_stays_lazy_until_first_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            startup_path = log_dir / "runtime_startup.log"
            target_path = log_dir / f"runtime_{date.today():%Y%m%d}_000000.log"
            handler = runtime_logging.DailyRuntimeFileHandler(startup_path, max_bytes=0)
            try:
                handler._switch_to_date(date.today())

                self.assertFalse(startup_path.exists())
                self.assertFalse(target_path.exists())

                handler.emit(self._record("after switch"))

                self.assertTrue(target_path.exists())
                self.assertIn("after switch", target_path.read_text(encoding="utf-8"))
            finally:
                handler.close()


class RuntimeLogCleanupTests(unittest.TestCase):
    def test_deletes_only_logs_older_than_seven_days(self):
        now = datetime(2026, 7, 30, 10, 31)
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            old_named_log = log_dir / "runtime_20260722_103000.log"
            retained_log = log_dir / "runtime_20260723_103100.log"
            old_generic_log = log_dir / "portfolio_nav_fetcher.log"
            unrelated_file = log_dir / "snapshot.csv"

            for path in (old_named_log, retained_log, old_generic_log, unrelated_file):
                path.touch()

            old_timestamp = datetime(2026, 7, 20, 12, 0).timestamp()
            os.utime(old_generic_log, (old_timestamp, old_timestamp))

            with (
                patch.object(runtime_logging, "RUNTIME_LOG_DIR", log_dir),
                patch.object(runtime_logging, "_shared_file_handler", None),
            ):
                deleted, failed = runtime_logging.cleanup_runtime_logs(7, now=now)

            self.assertEqual(set(deleted), {old_named_log, old_generic_log})
            self.assertEqual(failed, [])
            self.assertFalse(old_named_log.exists())
            self.assertFalse(old_generic_log.exists())
            self.assertTrue(retained_log.exists())
            self.assertTrue(unrelated_file.exists())

    def test_never_deletes_active_log_file(self):
        now = datetime(2026, 7, 30, 10, 31)
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            active_log = log_dir / "runtime_20260701_000000.log"
            active_log.touch()

            handler = type("Handler", (), {"baseFilename": str(active_log)})()
            with (
                patch.object(runtime_logging, "RUNTIME_LOG_DIR", log_dir),
                patch.object(runtime_logging, "_shared_file_handler", handler),
            ):
                deleted, failed = runtime_logging.cleanup_runtime_logs(7, now=now)

            self.assertEqual(deleted, [])
            self.assertEqual(failed, [])
            self.assertTrue(active_log.exists())

    def test_rejects_invalid_retention_period(self):
        with self.assertRaises(ValueError):
            runtime_logging.cleanup_runtime_logs(0)


if __name__ == "__main__":
    unittest.main()
