import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ops.backfill_bigquery_returns import (
    SourceSpec,
    _duplicate_keys,
    build_backfill_plan,
    filter_sources,
    iter_dates,
    parse_account_filter,
    resolve_legacy_sources,
    validate_date_range,
)


def write_snapshot(path: Path, target_date: date) -> None:
    rows = []
    start_date = target_date - timedelta(days=29)
    for index in range(30):
        current_date = start_date + timedelta(days=index)
        reference = 1 + index * 0.001 + index * index * 0.00001
        rows.extend(
            [
                {
                    "timestamp": f"{current_date.isoformat()} 00:01:00",
                    "net_value": reference - 0.0002,
                },
                {
                    "timestamp": f"{current_date.isoformat()} 09:30:00",
                    "net_value": reference,
                },
                {
                    "timestamp": f"{current_date.isoformat()} 23:59:00",
                    "net_value": reference + 0.0003,
                },
            ]
        )
    pd.DataFrame(rows).to_csv(path, index=False)


class BigQueryBackfillTests(unittest.TestCase):
    def test_date_range_rejects_today_future_reverse_and_large_ranges(self):
        today = date(2026, 7, 24)
        validate_date_range(
            date(2026, 7, 22),
            date(2026, 7, 23),
            today_utc=today,
        )
        with self.assertRaisesRegex(ValueError, "UTC yesterday"):
            validate_date_range(today, today, today_utc=today)
        with self.assertRaisesRegex(ValueError, "must not be after"):
            validate_date_range(
                date(2026, 7, 23),
                date(2026, 7, 22),
                today_utc=today,
            )
        with self.assertRaisesRegex(ValueError, "maximum"):
            validate_date_range(
                date(2025, 1, 1),
                date(2026, 1, 2),
                today_utc=today,
            )

    def test_iter_dates_is_inclusive(self):
        self.assertEqual(
            list(iter_dates(date(2026, 7, 21), date(2026, 7, 23))),
            [date(2026, 7, 21), date(2026, 7, 22), date(2026, 7, 23)],
        )

    def test_legacy_resolver_uses_configured_accounts_and_expected_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_dir = Path(temp_dir)
            (monitor_dir / "Binance_config.yaml").write_text(
                "Brioni_27:\n"
                "  initial_unit: 25\n"
                "  account_type: account\n"
                "Blacklist:\n"
                "  - BTC\n",
                encoding="utf-8",
            )
            specs = resolve_legacy_sources([monitor_dir])

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].account_name, "Brioni_27")
        self.assertEqual(specs[0].account, "Brioni27")
        self.assertEqual(
            specs[0].csv_path.name,
            "Binance_Brioni_27_minute_log.csv",
        )

    def test_account_filter_accepts_raw_normalized_and_comma_separated_names(self):
        specs = [
            SourceSpec("Brioni_27", "legacy", Path("/tmp/brioni.csv")),
            SourceSpec("BV_5", "legacy", Path("/tmp/bv.csv")),
        ]
        requested = parse_account_filter(["Brioni_27,BV5"])
        selected = filter_sources(specs, requested)
        self.assertEqual([spec.account for spec in selected], ["Brioni27", "BV5"])

        with self.assertRaisesRegex(ValueError, "unknown accounts"):
            filter_sources(specs, {"Unknown1"})

    def test_plan_marks_missing_key_ready_and_existing_key_skipped(self):
        target_date = date(2026, 7, 22)
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "Binance_Brioni_27_minute_log.csv"
            write_snapshot(csv_path, target_date)
            spec = SourceSpec("Brioni_27", "legacy", csv_path)

            rows, audit = build_backfill_plan(
                [spec],
                start_date=target_date,
                end_date=target_date,
                existing_counts={},
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["account"], "Brioni27")
            self.assertEqual(audit.loc[0, "status"], "ready")

            rows, audit = build_backfill_plan(
                [spec],
                start_date=target_date,
                end_date=target_date,
                existing_counts={(target_date, "Brioni27"): 1},
            )
            self.assertEqual(rows, [])
            self.assertEqual(audit.loc[0, "status"], "already_exists")
            self.assertEqual(audit.loc[0, "existing_count"], 1)

    def test_replace_existing_selects_calculable_row(self):
        target_date = date(2026, 7, 22)
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "snapshot.csv"
            write_snapshot(csv_path, target_date)
            rows, audit = build_backfill_plan(
                [SourceSpec("Brioni_27", "legacy", csv_path)],
                start_date=target_date,
                end_date=target_date,
                existing_counts={(target_date, "Brioni27"): 1},
                replace_existing=True,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(audit.loc[0, "status"], "ready")
        self.assertIn("replace", audit.loc[0, "reason"])

    def test_missing_snapshot_is_audited_for_each_date(self):
        start_date = date(2026, 7, 21)
        end_date = date(2026, 7, 22)
        rows, audit = build_backfill_plan(
            [SourceSpec("Missing_1", "legacy", Path("/tmp/not-present.csv"))],
            start_date=start_date,
            end_date=end_date,
            existing_counts={},
        )
        self.assertEqual(rows, [])
        self.assertEqual(audit["status"].tolist(), ["source_error", "source_error"])
        self.assertTrue(audit["reason"].str.contains("not found").all())

    def test_duplicate_target_keys_are_detected(self):
        duplicates = _duplicate_keys(
            {
                (date(2026, 7, 21), "Brioni27"): 1,
                (date(2026, 7, 22), "Brioni27"): 2,
            }
        )
        self.assertEqual(
            duplicates,
            [(date(2026, 7, 22), "Brioni27", 2)],
        )


if __name__ == "__main__":
    unittest.main()
