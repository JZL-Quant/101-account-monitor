"""Build and persist daily Binance return rows for BigQuery."""

from __future__ import annotations

import math
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd


RETURN_COLUMNS = (
    "date",
    "net_value_reference",
    "net_value_open",
    "net_value_close",
    "net_value_mean",
    "intraday_return",
    "daily_return_annualized",
    "7d_return_annualized",
    "30d_return_annualized",
    "7d_sharpe",
    "30d_sharpe",
    "account",
)

FLOAT_COLUMNS = RETURN_COLUMNS[1:-1]
BIGQUERY_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def completed_reference_date(now_utc: datetime | None = None) -> date:
    """Return the newest UTC date whose 09:00-10:00 reference window is complete."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.date() if now.hour >= 10 else now.date() - timedelta(days=1)


def normalize_account_name(account_name: str) -> str:
    """Keep compatibility with legacy BN_Return_temp account labels."""
    return str(account_name).replace("_", "")


def _finite_float(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"non-finite numeric value: {value!r}")
    return result


def calculate_return_row(
    snapshot_df: pd.DataFrame,
    target_date: date,
    account_name: str,
) -> dict[str, Any]:
    """Calculate one BN_Return_temp-compatible row from a minute snapshot."""
    required_columns = {"timestamp", "net_value"}
    missing = required_columns.difference(snapshot_df.columns)
    if missing:
        raise ValueError(f"missing snapshot columns: {', '.join(sorted(missing))}")

    df = snapshot_df.loc[:, ["timestamp", "net_value"]].copy()
    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        format="mixed",
        errors="coerce",
        utc=True,
    ).dt.tz_convert(None)
    df["net_value"] = pd.to_numeric(df["net_value"], errors="coerce")
    df = df.dropna(subset=["timestamp", "net_value"])
    df = df[np.isfinite(df["net_value"]) & (df["net_value"] > 0)]
    if df.empty:
        raise ValueError("no valid positive net-value snapshots")

    start_date = target_date - timedelta(days=29)
    df["date"] = df["timestamp"].dt.date
    df = df[(df["date"] >= start_date) & (df["date"] <= target_date)]
    if df.empty:
        raise ValueError(f"no snapshots in {start_date}..{target_date}")

    df = df.sort_values("timestamp").reset_index(drop=True)
    df["hour"] = df["timestamp"].dt.hour

    morning_data = df[(df["hour"] >= 9) & (df["hour"] < 10)]
    daily_reference = morning_data.groupby("date")["net_value"].median().rename("reference")
    daily_full = df.groupby("date")["net_value"].agg(
        net_value_open="first",
        net_value_close="last",
        net_value_mean="mean",
    )
    daily = daily_full.join(daily_reference, how="inner").sort_index()

    if target_date not in daily.index:
        raise ValueError(f"target date {target_date} has no complete reference data")
    if len(daily) < 30:
        raise ValueError(f"only {len(daily)} daily reference points; 30 required")

    daily = daily.loc[:target_date].tail(30)
    if len(daily) < 30:
        raise ValueError(f"only {len(daily)} daily reference points through {target_date}; 30 required")

    references = daily["reference"]
    daily_return = references.iloc[-1] / references.iloc[-2] - 1
    returns_7d = references.tail(7).pct_change().dropna()
    returns_30d = references.tail(30).pct_change().dropna()
    if len(returns_7d) < 3 or len(returns_30d) < 10:
        raise ValueError("insufficient daily returns for annualized metrics")

    target = daily.loc[target_date]
    if target["net_value_open"] <= 0:
        raise ValueError("target-date opening net value is not positive")

    cumulative_7d = (1 + returns_7d).prod() - 1
    cumulative_30d = (1 + returns_30d).prod() - 1
    std_7d = returns_7d.std()
    std_30d = returns_30d.std()
    if not math.isfinite(std_7d) or std_7d <= 0:
        raise ValueError("7-day return standard deviation is not positive")
    if not math.isfinite(std_30d) or std_30d <= 0:
        raise ValueError("30-day return standard deviation is not positive")

    row = {
        "date": target_date.isoformat(),
        "net_value_reference": target["reference"],
        "net_value_open": target["net_value_open"],
        "net_value_close": target["net_value_close"],
        "net_value_mean": target["net_value_mean"],
        "intraday_return": target["net_value_close"] / target["net_value_open"] - 1,
        "daily_return_annualized": daily_return * 365,
        "7d_return_annualized": cumulative_7d / len(returns_7d) * 365,
        "30d_return_annualized": cumulative_30d / len(returns_30d) * 365,
        "7d_sharpe": returns_7d.mean() / std_7d * np.sqrt(365),
        "30d_sharpe": returns_30d.mean() / std_30d * np.sqrt(365),
        "account": normalize_account_name(account_name),
    }
    for column in FLOAT_COLUMNS:
        row[column] = _finite_float(row[column])
    return row


def build_binance_return_rows(
    accounts: dict[str, dict[str, Any]],
    snapshots: dict[str, pd.DataFrame],
    target_date: date,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Build rows for configured Binance accounts and report skipped accounts."""
    normalized_names: dict[str, str] = {}
    for account_name, account_info in accounts.items():
        if account_info.get("exchange_id", "binance").lower() != "binance":
            continue
        normalized = normalize_account_name(account_name)
        previous = normalized_names.get(normalized)
        if previous is not None and previous != account_name:
            raise ValueError(
                f"normalized Binance account collision: {previous!r} and "
                f"{account_name!r} both map to {normalized!r}"
            )
        normalized_names[normalized] = account_name

    rows = []
    skipped = {}
    for normalized, account_name in normalized_names.items():
        snapshot_df = snapshots.get(account_name)
        if snapshot_df is None or snapshot_df.empty:
            skipped[account_name] = "snapshot is missing or empty"
            continue
        try:
            rows.append(calculate_return_row(snapshot_df, target_date, account_name))
        except (KeyError, TypeError, ValueError) as exc:
            skipped[account_name] = str(exc)

    rows.sort(key=lambda row: row["account"])
    return rows, skipped


def build_merge_sql(target_table: str, staging_table: str) -> str:
    """Build the idempotent daily-row MERGE statement."""
    update_columns = [column for column in RETURN_COLUMNS if column not in {"date", "account"}]
    update_clause = ",\n        ".join(
        f"target.`{column}` = source.`{column}`" for column in update_columns
    )
    insert_columns = ", ".join(f"`{column}`" for column in RETURN_COLUMNS)
    insert_values = ", ".join(f"source.`{column}`" for column in RETURN_COLUMNS)
    return f"""
MERGE `{target_table}` AS target
USING `{staging_table}` AS source
ON target.date = source.date AND target.account = source.account
WHEN MATCHED THEN
  UPDATE SET
        {update_clause}
WHEN NOT MATCHED THEN
  INSERT ({insert_columns})
  VALUES ({insert_values})
"""


def _validate_bigquery_identifier(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not value or not BIGQUERY_IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"invalid BigQuery {label}: {value!r}")
    return value


def merge_rows_to_bigquery(
    rows: list[dict[str, Any]],
    *,
    project_id: str,
    dataset: str,
    table: str,
) -> dict[str, Any]:
    """Load rows into a unique staging table and MERGE by (date, account)."""
    if not rows:
        return {"row_count": 0, "job_id": None}

    project_id = _validate_bigquery_identifier(project_id, "project ID")
    dataset = _validate_bigquery_identifier(dataset, "dataset")
    table = _validate_bigquery_identifier(table, "table")

    # Import lazily so deployments with BigQuery disabled retain their current
    # dependency and startup behavior.
    from google.cloud import bigquery

    client = bigquery.Client(project=project_id)
    target_table = f"{project_id}.{dataset}.{table}"
    staging_name = f"{table}_stage_{uuid.uuid4().hex}"
    staging_table = f"{project_id}.{dataset}.{staging_name}"

    schema = [
        bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
        *(
            bigquery.SchemaField(column, "FLOAT64", mode="REQUIRED")
            for column in FLOAT_COLUMNS
        ),
        bigquery.SchemaField("account", "STRING", mode="REQUIRED"),
    ]
    load_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )

    try:
        staging_resource = bigquery.Table(staging_table, schema=schema)
        staging_resource.expires = datetime.now(timezone.utc) + timedelta(days=1)
        client.create_table(staging_resource)
        load_job = client.load_table_from_json(rows, staging_table, job_config=load_config)
        load_job.result()

        merge_sql = build_merge_sql(target_table, staging_table)
        merge_job = client.query(merge_sql)
        merge_job.result()
        return {"row_count": len(rows), "job_id": merge_job.job_id}
    finally:
        client.delete_table(staging_table, not_found_ok=True)
