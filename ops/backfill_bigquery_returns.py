"""Safely backfill missing daily Binance return rows into BigQuery.

The command is dry-run by default.  It reads historical minute snapshots,
calculates rows with the production return calculator, compares them with the
target table at ``(date, account)`` grain, and writes an audit CSV.  BigQuery is
only modified when ``--execute`` is supplied.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd
import yaml

from config.settings import (
    ACCOUNTS_CONFIG_PATH,
    BIGQUERY_CREDENTIALS_PATH,
    BIGQUERY_DATASET,
    BIGQUERY_PROJECT_ID,
    BIGQUERY_RETURN_TABLE,
    PROJECT_ROOT,
    RUNTIME_LOG_DIR,
)
from core.bigquery_returns import (
    _validate_bigquery_identifier,
    calculate_return_row,
    create_bigquery_client,
    merge_rows_to_bigquery,
    normalize_account_name,
)


MAX_BACKFILL_DAYS = 366
LEGACY_MONITOR_DIRS = (
    PROJECT_ROOT.parent / "Binance_monitor",
    PROJECT_ROOT.parent / "Binance_monitor_B",
)
AUDIT_COLUMNS = (
    "date",
    "account",
    "source",
    "source_file",
    "status",
    "reason",
    "existing_count",
)


@dataclass(frozen=True)
class SourceSpec:
    account_name: str
    source: str
    csv_path: Path

    @property
    def account(self) -> str:
        return normalize_account_name(self.account_name)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


def iter_dates(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def validate_date_range(
    start_date: date,
    end_date: date,
    *,
    today_utc: date | None = None,
) -> None:
    today = today_utc or datetime.now(timezone.utc).date()
    if start_date > end_date:
        raise ValueError("start date must not be after end date")
    if end_date >= today:
        raise ValueError(
            f"end date must be no later than UTC yesterday ({today - timedelta(days=1)})"
        )
    day_count = (end_date - start_date).days + 1
    if day_count > MAX_BACKFILL_DAYS:
        raise ValueError(
            f"date range contains {day_count} days; maximum is {MAX_BACKFILL_DAYS}"
        )


def _load_yaml_account_names(config_path: Path) -> list[str]:
    if not config_path.is_file():
        raise FileNotFoundError(f"account config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise ValueError(f"account config must be a mapping: {config_path}")
    return [
        str(account_name)
        for account_name, account_info in config.items()
        if isinstance(account_info, dict)
    ]


def resolve_legacy_sources(
    monitor_dirs: Sequence[Path] = LEGACY_MONITOR_DIRS,
) -> list[SourceSpec]:
    specs: list[SourceSpec] = []
    for monitor_dir in monitor_dirs:
        config_path = monitor_dir / "Binance_config.yaml"
        for account_name in _load_yaml_account_names(config_path):
            specs.append(
                SourceSpec(
                    account_name=account_name,
                    source="legacy",
                    csv_path=monitor_dir / f"Binance_{account_name}_minute_log.csv",
                )
            )
    return _validate_source_specs(specs)


def resolve_account_monitor_sources(
    config_path: Path = ACCOUNTS_CONFIG_PATH,
) -> list[SourceSpec]:
    # Import lazily because AccountRegistry loads all exchange adapters.
    from core.account_registry import AccountRegistry

    accounts = AccountRegistry(str(config_path)).local_accounts()
    specs = [
        SourceSpec(
            account_name=account_name,
            source="account-monitor",
            csv_path=Path(account_info["minute_snapshot_file"]),
        )
        for account_name, account_info in accounts.items()
        if str(account_info.get("exchange_id", "binance")).lower() == "binance"
    ]
    return _validate_source_specs(specs)


def _validate_source_specs(specs: Sequence[SourceSpec]) -> list[SourceSpec]:
    by_normalized_name: dict[str, SourceSpec] = {}
    for spec in specs:
        previous = by_normalized_name.get(spec.account)
        if previous is not None:
            raise ValueError(
                "normalized account collision: "
                f"{previous.account_name!r} ({previous.csv_path}) and "
                f"{spec.account_name!r} ({spec.csv_path}) both map to "
                f"{spec.account!r}"
            )
        by_normalized_name[spec.account] = spec
    return sorted(specs, key=lambda item: item.account)


def parse_account_filter(values: Sequence[str] | None) -> set[str] | None:
    if not values:
        return None
    accounts = {
        normalize_account_name(part.strip())
        for value in values
        for part in value.split(",")
        if part.strip()
    }
    return accounts or None


def filter_sources(
    specs: Sequence[SourceSpec],
    requested_accounts: set[str] | None,
) -> list[SourceSpec]:
    if requested_accounts is None:
        return list(specs)
    known_accounts = {spec.account for spec in specs}
    unknown = sorted(requested_accounts - known_accounts)
    if unknown:
        raise ValueError(f"unknown accounts for selected source: {', '.join(unknown)}")
    return [spec for spec in specs if spec.account in requested_accounts]


def _target_table(project_id: str, dataset: str, table: str) -> str:
    project_id = _validate_bigquery_identifier(project_id, "project ID")
    dataset = _validate_bigquery_identifier(dataset, "dataset")
    table = _validate_bigquery_identifier(table, "table")
    return f"{project_id}.{dataset}.{table}"


def query_existing_key_counts(
    client,
    *,
    project_id: str,
    dataset: str,
    table: str,
    start_date: date,
    end_date: date,
) -> dict[tuple[date, str], int]:
    from google.cloud import bigquery

    target_table = _target_table(project_id, dataset, table)
    query = f"""
SELECT date, account, COUNT(*) AS row_count
FROM `{target_table}`
WHERE date BETWEEN @start_date AND @end_date
GROUP BY date, account
"""
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
        ]
    )
    return {
        (row.date, str(row.account)): int(row.row_count)
        for row in client.query(query, job_config=job_config).result()
    }


def _read_snapshot_window(
    csv_path: Path,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    snapshot = pd.read_csv(csv_path, usecols=["timestamp", "net_value"])
    snapshot["timestamp"] = pd.to_datetime(
        snapshot["timestamp"],
        format="mixed",
        errors="coerce",
        utc=True,
    )
    snapshot["net_value"] = pd.to_numeric(snapshot["net_value"], errors="coerce")
    snapshot = snapshot.dropna(subset=["timestamp", "net_value"])
    window_start = start_date - timedelta(days=29)
    row_dates = snapshot["timestamp"].dt.date
    return snapshot[(row_dates >= window_start) & (row_dates <= end_date)].copy()


def build_backfill_plan(
    specs: Sequence[SourceSpec],
    *,
    start_date: date,
    end_date: date,
    existing_counts: Mapping[tuple[date, str], int],
    replace_existing: bool = False,
) -> tuple[list[dict], pd.DataFrame]:
    rows: list[dict] = []
    audit_records: list[dict] = []
    target_dates = list(iter_dates(start_date, end_date))

    for spec in specs:
        base_record = {
            "account": spec.account,
            "source": spec.source,
            "source_file": str(spec.csv_path),
        }
        try:
            if not spec.csv_path.is_file():
                raise FileNotFoundError(f"snapshot file not found: {spec.csv_path}")
            snapshot = _read_snapshot_window(spec.csv_path, start_date, end_date)
        except Exception as exc:
            for target_date in target_dates:
                audit_records.append(
                    {
                        "date": target_date.isoformat(),
                        **base_record,
                        "status": "source_error",
                        "reason": str(exc),
                        "existing_count": existing_counts.get(
                            (target_date, spec.account), 0
                        ),
                    }
                )
            continue

        for target_date in target_dates:
            existing_count = existing_counts.get((target_date, spec.account), 0)
            try:
                row = calculate_return_row(snapshot, target_date, spec.account_name)
            except (KeyError, TypeError, ValueError) as exc:
                audit_records.append(
                    {
                        "date": target_date.isoformat(),
                        **base_record,
                        "status": "uncomputable",
                        "reason": str(exc),
                        "existing_count": existing_count,
                    }
                )
                continue

            if existing_count and not replace_existing:
                status = "already_exists"
                reason = "target key already exists; skipped"
            else:
                status = "ready"
                reason = (
                    "will replace existing target key"
                    if existing_count
                    else "target key is missing"
                )
                rows.append(row)

            audit_records.append(
                {
                    "date": target_date.isoformat(),
                    **base_record,
                    "status": status,
                    "reason": reason,
                    "existing_count": existing_count,
                }
            )

    audit = pd.DataFrame(audit_records, columns=AUDIT_COLUMNS)
    if not audit.empty:
        audit = audit.sort_values(["date", "account"]).reset_index(drop=True)
    rows.sort(key=lambda row: (row["date"], row["account"]))
    return rows, audit


def default_report_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return RUNTIME_LOG_DIR / f"bigquery_backfill_{timestamp}.csv"


def write_audit_report(audit: pd.DataFrame, report_path: Path) -> Path:
    report_path = report_path.expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    audit.to_csv(report_path, index=False, encoding="utf-8")
    return report_path


def _duplicate_keys(
    existing_counts: Mapping[tuple[date, str], int],
) -> list[tuple[date, str, int]]:
    return sorted(
        (
            (target_date, account, count)
            for (target_date, account), count in existing_counts.items()
            if count > 1
        ),
        key=lambda item: (item[0], item[1]),
    )


def _print_summary(audit: pd.DataFrame, *, row_count: int, execute: bool) -> None:
    print(f"mode: {'execute' if execute else 'dry-run'}")
    print(f"calculated rows selected for merge: {row_count}")
    if audit.empty:
        print("audit: no source accounts selected")
        return
    print("audit status counts:")
    for status, count in audit["status"].value_counts().sort_index().items():
        print(f"  {status}: {count}")
    by_date = audit.pivot_table(
        index="date",
        columns="status",
        values="account",
        aggfunc="count",
        fill_value=0,
    )
    print("per-date status counts:")
    print(by_date.to_string())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill missing BN_Return_temp rows from minute snapshots. "
            "The default mode is dry-run."
        )
    )
    parser.add_argument("--start-date", required=True, type=parse_date)
    parser.add_argument("--end-date", required=True, type=parse_date)
    parser.add_argument(
        "--source",
        choices=("legacy", "account-monitor"),
        default="legacy",
        help="snapshot source; legacy preserves the historical valuation scope",
    )
    parser.add_argument(
        "--accounts",
        nargs="+",
        help="optional account names, separated by spaces or commas",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="recalculate and MERGE existing keys as well as missing keys",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="calculate and report only (default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="write selected rows to BigQuery",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="audit CSV path (default: runtime_logs/bigquery_backfill_TIMESTAMP.csv)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_date_range(args.start_date, args.end_date)
        requested_accounts = parse_account_filter(args.accounts)
        if args.source == "legacy":
            specs = resolve_legacy_sources()
        else:
            specs = resolve_account_monitor_sources()
        specs = filter_sources(specs, requested_accounts)

        client = create_bigquery_client(
            BIGQUERY_PROJECT_ID,
            BIGQUERY_CREDENTIALS_PATH,
        )
        existing_counts = query_existing_key_counts(
            client,
            project_id=BIGQUERY_PROJECT_ID,
            dataset=BIGQUERY_DATASET,
            table=BIGQUERY_RETURN_TABLE,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        duplicates = _duplicate_keys(existing_counts)
        if duplicates:
            preview = ", ".join(
                f"{target_date}/{account}={count}"
                for target_date, account, count in duplicates[:10]
            )
            print(
                f"WARNING: target contains {len(duplicates)} duplicate keys: {preview}",
                file=sys.stderr,
            )
            if args.execute:
                raise RuntimeError(
                    "execute aborted because duplicate target keys must be repaired first"
                )

        rows, audit = build_backfill_plan(
            specs,
            start_date=args.start_date,
            end_date=args.end_date,
            existing_counts=existing_counts,
            replace_existing=args.replace_existing,
        )
        if args.execute and rows:
            result = merge_rows_to_bigquery(
                rows,
                project_id=BIGQUERY_PROJECT_ID,
                dataset=BIGQUERY_DATASET,
                table=BIGQUERY_RETURN_TABLE,
                credentials_path=BIGQUERY_CREDENTIALS_PATH,
            )
            audit.loc[audit["status"] == "ready", "status"] = "uploaded"
            audit.loc[audit["status"] == "uploaded", "reason"] = (
                f"BigQuery MERGE completed; job_id={result['job_id']}"
            )

        report_path = write_audit_report(audit, args.report or default_report_path())
        _print_summary(audit, row_count=len(rows), execute=args.execute)
        print(f"audit report: {report_path}")
        if args.execute and not rows:
            print("BigQuery unchanged: no rows were ready for merge")
        elif not args.execute:
            print("BigQuery unchanged: dry-run mode")
        return 0
    except (FileNotFoundError, RuntimeError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
