import math
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.bigquery_returns import (
    RETURN_COLUMNS,
    _validate_bigquery_identifier,
    build_binance_return_rows,
    build_merge_sql,
    calculate_return_row,
    completed_reference_date,
    create_bigquery_client,
)


def build_snapshot(target_date: date) -> pd.DataFrame:
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
    return pd.DataFrame(rows)


class BigQueryReturnCalculationTests(unittest.TestCase):
    def test_completed_reference_date_uses_utc_window(self):
        before_window_end = datetime(2026, 7, 24, 9, 59, tzinfo=timezone.utc)
        after_window_end = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(completed_reference_date(before_window_end), date(2026, 7, 23))
        self.assertEqual(completed_reference_date(after_window_end), date(2026, 7, 24))

    def test_calculation_matches_legacy_table_shape_and_units(self):
        target_date = date(2026, 7, 22)
        snapshot = build_snapshot(target_date)
        row = calculate_return_row(snapshot, target_date, "Brioni_27")

        self.assertEqual(tuple(row), RETURN_COLUMNS)
        self.assertEqual(row["date"], "2026-07-22")
        self.assertEqual(row["account"], "Brioni27")

        references = (
            snapshot[snapshot["timestamp"].str.contains(" 09:30:00")]
            .set_index(pd.Index(range(30)))["net_value"]
        )
        expected_daily = (references.iloc[-1] / references.iloc[-2] - 1) * 365
        returns_7d = references.tail(7).pct_change().dropna()
        returns_30d = references.pct_change().dropna()
        expected_7d = ((1 + returns_7d).prod() - 1) / len(returns_7d) * 365
        expected_30d = ((1 + returns_30d).prod() - 1) / len(returns_30d) * 365

        self.assertAlmostEqual(row["daily_return_annualized"], expected_daily)
        self.assertAlmostEqual(row["7d_return_annualized"], expected_7d)
        self.assertAlmostEqual(row["30d_return_annualized"], expected_30d)
        self.assertTrue(math.isfinite(row["7d_sharpe"]))
        self.assertTrue(math.isfinite(row["30d_sharpe"]))

    def test_builder_excludes_gate_and_reports_missing_binance_snapshot(self):
        target_date = date(2026, 7, 22)
        accounts = {
            "Brioni_27": {"exchange_id": "binance"},
            "Kiton_1": {"exchange_id": "binance"},
            "Vicuna_201": {"exchange_id": "gate"},
        }
        rows, skipped = build_binance_return_rows(
            accounts,
            {
                "Brioni_27": build_snapshot(target_date),
                "Vicuna_201": build_snapshot(target_date),
            },
            target_date,
        )
        self.assertEqual([row["account"] for row in rows], ["Brioni27"])
        self.assertEqual(set(skipped), {"Kiton_1"})
        self.assertNotIn("Vicuna_201", skipped)

    def test_normalized_account_collision_is_rejected(self):
        accounts = {
            "AB_C": {"exchange_id": "binance"},
            "A_BC": {"exchange_id": "binance"},
        }
        with self.assertRaisesRegex(ValueError, "collision"):
            build_binance_return_rows(accounts, {}, date(2026, 7, 22))

    def test_merge_is_keyed_by_date_and_account(self):
        sql = build_merge_sql(
            "project.dataset.BN_Return_temp",
            "project.dataset.BN_Return_temp_stage_123",
        )
        self.assertIn(
            "ON target.date = source.date AND target.account = source.account",
            sql,
        )
        self.assertIn("WHEN MATCHED THEN", sql)
        self.assertIn("WHEN NOT MATCHED THEN", sql)

    def test_bigquery_identifiers_are_restricted(self):
        self.assertEqual(
            _validate_bigquery_identifier("applied-groove-464707-r8", "project ID"),
            "applied-groove-464707-r8",
        )
        with self.assertRaisesRegex(ValueError, "invalid BigQuery table"):
            _validate_bigquery_identifier("table` DROP TABLE x", "table")

    def test_bigquery_client_loads_explicit_service_account_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            key_path = Path(temp_dir) / "service-account.json"
            key_path.touch()
            credentials = object()

            with (
                patch(
                    "google.oauth2.service_account.Credentials.from_service_account_file",
                    return_value=credentials,
                ) as credential_loader,
                patch("google.cloud.bigquery.Client") as client_class,
            ):
                client = create_bigquery_client("test-project", key_path)

            credential_loader.assert_called_once_with(str(key_path))
            client_class.assert_called_once_with(
                project="test-project",
                credentials=credentials,
            )
            self.assertIs(client, client_class.return_value)

    def test_bigquery_client_rejects_missing_credentials_file(self):
        missing_path = Path("/definitely/missing/service-account.json")
        with self.assertRaisesRegex(FileNotFoundError, "credentials file not found"):
            create_bigquery_client("test-project", missing_path)


if __name__ == "__main__":
    unittest.main()
