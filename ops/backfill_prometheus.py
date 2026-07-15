#!/usr/bin/env python3
"""Detect Prometheus gaps from minute CSV files and build backfill TSDB blocks."""

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    ACCOUNTS_CONFIG_PATH,
    GRAFANA_PROMETHEUS_INSTANCE_ACCOUNT_MONITOR,
    MINUTE_SNAPSHOT_DIR,
    MIN_VALID_CALCULATION_VALUE,
    PROJECT_ROOT,
)


DEFAULT_PROMETHEUS_URL = "http://127.0.0.1:9090"
DEFAULT_JOB = "account-monitor"


def parse_args():
    parser = argparse.ArgumentParser(
        description="检查分钟 CSV 与 Prometheus 的 actual_equity 缺口，并生成历史数据回灌包。"
    )
    parser.add_argument("--account", action="append", default=[], help="Account name; repeatable. Default: all accounts.")
    parser.add_argument("--start", required=True, help="Inclusive ISO timestamp, e.g. 2026-07-14T00:00:00Z.")
    parser.add_argument("--end", required=True, help="Inclusive ISO timestamp; must be outside the recent safety window.")
    parser.add_argument("--csv-timezone", default="UTC", help="Timezone used by naive CSV timestamps. Default: UTC.")
    parser.add_argument("--prometheus-url", default=DEFAULT_PROMETHEUS_URL)
    parser.add_argument("--instance", default=GRAFANA_PROMETHEUS_INSTANCE_ACCOUNT_MONITOR)
    parser.add_argument("--job", default=DEFAULT_JOB)
    parser.add_argument("--label", action="append", default=[], metavar="NAME=VALUE", help="Override/add a series label; repeatable.")
    parser.add_argument("--min-age-hours", type=float, default=3.0, help="Refuse newer samples. Default: 3 hours.")
    parser.add_argument("--config", type=Path, default=ACCOUNTS_CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "backfill_output")
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="One-click: detect gaps and create a package ready for Prometheus import.",
    )
    parser.add_argument("--write-openmetrics", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--create-blocks", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--promtool", default="promtool", help="promtool executable path. Default: promtool.")
    return parser.parse_args()


def parse_timestamp(value):
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def safe_label(value):
    return re.sub(r"\W+", "_", str(value or "")).strip("_")


def exchange_label(value):
    value = str(value or "Binance").strip() or "Binance"
    return safe_label(value[:1].upper() + value[1:]) or "Exchange"


def load_accounts(config_path, selected_names):
    import yaml

    with config_path.open("r", encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file) or {}
    accounts = {}
    for name, info in raw.items():
        if not isinstance(info, dict):
            continue
        name = str(name)
        if selected_names and name not in selected_names:
            continue
        label = exchange_label(info.get("exchange", "Binance"))
        ccy = safe_label(str(info.get("ccy", "USDT")).upper()) or "USDT"
        accounts[name] = {
            "metric": f"{label}_{name}_actual_equity",
            "csv": MINUTE_SNAPSHOT_DIR / f"{label}_{name}_{ccy}_minute_snapshot.csv",
        }
    missing = set(selected_names) - set(accounts)
    if missing:
        raise ValueError(f"accounts not found in config: {', '.join(sorted(missing))}")
    return accounts


def prometheus_get(base_url, path, params):
    url = f"{base_url.rstrip('/')}{path}?{urlencode(params, doseq=True)}"
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus request failed: {payload}")
    return payload["data"]


def label_selector(metric, labels):
    if not labels:
        return metric
    parts = []
    for key, value in sorted(labels.items()):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'{key}="{escaped}"')
    return f"{metric}{{{','.join(parts)}}}"


def query_existing_samples(prometheus_url, selector, start, end):
    timestamps = set()
    series_labels = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=7), end)
        duration_seconds = max(1, int((chunk_end - chunk_start).total_seconds()) + 1)
        data = prometheus_get(
            prometheus_url,
            "/api/v1/query",
            {
                "query": f"{selector}[{duration_seconds}s] @ {chunk_end.timestamp():.3f}",
                "time": chunk_end.timestamp(),
            },
        )
        for result in data.get("result", []):
            labels = {
                key: str(value)
                for key, value in result.get("metric", {}).items()
                if key != "__name__"
            }
            if labels not in series_labels:
                series_labels.append(labels)
            for timestamp, _value in result.get("values", []):
                sample_time = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
                if start <= sample_time <= end:
                    timestamps.add(sample_time.replace(second=0, microsecond=0))
        if chunk_end >= end:
            break
        chunk_start = chunk_end
    return timestamps, series_labels


def read_csv_samples(path, start, end, csv_timezone):
    import pandas as pd

    if not path.exists():
        raise FileNotFoundError(path)
    frame = pd.read_csv(path, usecols=["timestamp", "actual_equity"])
    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce")
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(ZoneInfo(csv_timezone), ambiguous="NaT", nonexistent="NaT")
    timestamps = timestamps.dt.tz_convert("UTC")
    values = pd.to_numeric(frame["actual_equity"], errors="coerce")

    samples = {}
    for timestamp, value in zip(timestamps, values):
        if pd.isna(timestamp) or pd.isna(value):
            continue
        numeric_value = float(value)
        if not math.isfinite(numeric_value) or numeric_value <= MIN_VALID_CALCULATION_VALUE:
            continue
        sample_time = timestamp.to_pydatetime().astimezone(timezone.utc).replace(second=0, microsecond=0)
        if start <= sample_time <= end:
            samples[sample_time] = numeric_value
    return samples


def parse_labels(raw_labels, instance, job):
    labels = {"instance": instance, "job": job}
    for item in raw_labels:
        if "=" not in item:
            raise ValueError(f"invalid --label, expected NAME=VALUE: {item}")
        key, value = item.split("=", 1)
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
            raise ValueError(f"invalid label name: {key}")
        labels[key] = value
    return {key: value for key, value in labels.items() if value != ""}


def choose_labels(configured_labels, discovered_series):
    if not discovered_series:
        return configured_labels
    matching = [
        labels for labels in discovered_series
        if all(labels.get(key) == value for key, value in configured_labels.items())
    ]
    candidates = matching or discovered_series
    unique = {tuple(sorted(labels.items())) for labels in candidates}
    if len(unique) > 1:
        raise ValueError("multiple Prometheus series matched; use --label to disambiguate")
    discovered = dict(next(iter(unique)))
    discovered.update(configured_labels)
    return discovered


def escape_openmetrics_label(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def write_openmetrics(path, prepared):
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for item in prepared:
            metric = item["metric"]
            output.write(f"# TYPE {metric} gauge\n")
            labels = ",".join(
                f'{key}="{escape_openmetrics_label(value)}"'
                for key, value in sorted(item["labels"].items())
            )
            label_text = f"{{{labels}}}" if labels else ""
            for timestamp, value in sorted(item["missing"].items()):
                output.write(f"{metric}{label_text} {value:.17g} {timestamp.timestamp():.3f}\n")
        output.write("# EOF\n")


def write_import_instructions(path, blocks_dir, openmetrics_path):
    path.write_text(
        "Prometheus 历史数据回灌包\n"
        "==========================\n\n"
        f"待导入数据目录: {blocks_dir}\n"
        f"中间转换文件: {openmetrics_path}\n\n"
        "导入步骤：\n"
        "1. 找到 Prometheus 的 --storage.tsdb.path。\n"
        "2. 备份完整的 Prometheus 数据目录。\n"
        "3. 停止 Prometheus。\n"
        "4. 将待导入数据目录中的每个 ULID 文件夹复制到 storage.tsdb.path。\n"
        "5. 启动 Prometheus，检查启动日志和 Grafana 曲线。\n\n"
        "不要重复导入同一个回灌包。\n",
        encoding="utf-8",
    )


def main():
    args = parse_args()
    try:
        start = parse_timestamp(args.start)
        end = parse_timestamp(args.end)
        if start > end:
            raise ValueError("--start must not be later than --end")
        safe_end = datetime.now(timezone.utc) - timedelta(hours=args.min_age_hours)
        if end > safe_end:
            raise ValueError(
                f"--end is inside the {args.min_age_hours:g}-hour safety window; "
                f"use an end time at or before {safe_end.isoformat()}"
            )
        ZoneInfo(args.csv_timezone)
        configured_labels = parse_labels(args.label, args.instance, args.job)
        accounts = load_accounts(args.config, args.account)
        if not accounts:
            raise ValueError("no accounts selected")

        prepared = []
        total_csv = total_existing = total_missing = 0
        for account_name, account in accounts.items():
            selector = label_selector(account["metric"], configured_labels)
            existing, discovered_series = query_existing_samples(
                args.prometheus_url, selector, start, end
            )
            csv_samples = read_csv_samples(account["csv"], start, end, args.csv_timezone)
            missing = {timestamp: value for timestamp, value in csv_samples.items() if timestamp not in existing}
            labels = choose_labels(configured_labels, discovered_series)
            prepared.append({**account, "account": account_name, "labels": labels, "missing": missing})
            total_csv += len(csv_samples)
            total_existing += len(existing)
            total_missing += len(missing)
            print(
                f"{account_name}: csv={len(csv_samples)}, prometheus_minutes={len(existing)}, "
                f"missing={len(missing)}, metric={account['metric']}"
            )

        print(f"TOTAL: csv={total_csv}, prometheus_minutes={total_existing}, missing={total_missing}")
        if total_missing == 0:
            print("No backfill data is required.")
            return 0
        prepare_package = args.prepare or args.create_blocks
        if not (args.write_openmetrics or prepare_package):
            print("检查完成，未修改任何数据。确认 missing 数量后添加 --prepare 生成回灌包。")
            return 0

        args.output_dir.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        openmetrics_path = args.output_dir / f"actual_equity_{run_id}.openmetrics"
        write_openmetrics(openmetrics_path, prepared)
        print(f"中间转换文件已生成: {openmetrics_path}")

        if prepare_package:
            blocks_dir = args.output_dir / f"blocks_{run_id}"
            subprocess.run(
                [args.promtool, "tsdb", "create-blocks-from", "openmetrics", str(openmetrics_path), str(blocks_dir)],
                check=True,
            )
            instructions_path = args.output_dir / f"IMPORT_{run_id}.txt"
            write_import_instructions(instructions_path, blocks_dir, openmetrics_path)
            print(f"回灌包已准备完成: {blocks_dir}")
            print(f"导入说明: {instructions_path}")
            print("脚本没有改动 Prometheus。请备份并停止 Prometheus 后再按说明导入。")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
