#!/usr/bin/env python3
"""Build a real-data return anomaly card and optionally send it to a test group."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.feishu.performance_summary_card import build_return_performance_card
from core.return_attention import build_return_performance_sections


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# 只填写测试群机器人的 Webhook；不要填写正式群地址。
FEISHU_TEST_BOT_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/af6b4e9f-9981-4c78-bc18-9b1bccc3e620"
ACCOUNT_MONITOR_BASE_URL = "http://127.0.0.1:7007"

PERIODS = (
    ("24h 收益率表现", "anomaly_24h_table", "ar24h", "annualized_return_24h"),
    ("7D 收益率表现", "anomaly_7d_table", "ar7d", "annualized_return_7d"),
)


def report_date_china():
    return datetime.now(timezone(timedelta(hours=8))).date()


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"Accept": "text/plain"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"读取 {url} 失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 {url}：{exc.reason}") from exc


def fetch_json(url: str) -> dict:
    try:
        return json.loads(fetch_text(url))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{url} 返回了无效 JSON") from exc


def parse_prometheus_metrics(content: str) -> dict[str, float]:
    values = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2 or "{" in parts[0]:
            continue
        try:
            values[parts[0]] = float(parts[1])
        except ValueError:
            continue
    return values


def exchange_metric_label(exchange: str) -> str:
    value = (exchange or "Binance").strip()
    titled = value[:1].upper() + value[1:] if value else "Binance"
    return re.sub(r"\W+", "_", titled).strip("_") or "Exchange"


def collect_account_returns() -> tuple[list[dict], list[str], int]:
    """Read the account monitor's already-calculated return gauges."""
    account_payload = fetch_json(f"{ACCOUNT_MONITOR_BASE_URL}/api/accounts")
    accounts = account_payload.get("accounts", [])
    if not isinstance(accounts, list):
        raise RuntimeError("/api/accounts 响应缺少 accounts 列表")
    metric_values = parse_prometheus_metrics(fetch_text(f"{ACCOUNT_MONITOR_BASE_URL}/metrics"))
    results = []
    skipped = []

    for account_info in accounts:
        account_name = str(account_info.get("name", "")).strip()
        exchange = str(account_info.get("exchange", "Binance")).strip()
        ccy = str(account_info.get("ccy", "USDT")).strip().upper()
        if not account_name:
            skipped.append("发现一个缺少名称的账户")
            continue
        metric_prefix = f"{exchange_metric_label(exchange)}_{account_name}"
        returns = {}
        missing_metrics = []
        for _, _, result_key, metric_suffix in PERIODS:
            metric_name = f"{metric_prefix}_{metric_suffix}"
            value = metric_values.get(metric_name)
            if value is None or not math.isfinite(value):
                returns[result_key] = None
                missing_metrics.append(metric_suffix)
            else:
                returns[result_key] = value
        if not any(value is not None for value in returns.values()):
            skipped.append(f"{account_name}: 目标收益率指标均无有效值")
            continue
        results.append(
            {
                "account_name": account_name,
                "exchange_id": exchange.lower(),
                "exchange_label": exchange,
                "ccy": ccy,
                "annualized_return_24h": returns.get("ar24h"),
                "annualized_return_7d": returns.get("ar7d"),
            }
        )
        if missing_metrics:
            skipped.append(f"{account_name}: 缺少 {', '.join(missing_metrics)}")

    return results, skipped, len(accounts)


def send(webhook_url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"飞书返回 HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接飞书：{exc.reason}") from exc

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"飞书返回了非 JSON 内容：{body[:500]}") from exc
    if result.get("code") != 0:
        raise RuntimeError(
            f"飞书发送失败：code={result.get('code')}, msg={result.get('msg')}"
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="读取 account monitor 已计算指标，预览或发送收益率表现总览卡片。"
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="真正发送；未指定时只输出卡片 JSON。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        results, skipped_accounts, configured_account_count = collect_account_returns()
    except RuntimeError as exc:
        print(f"读取 account monitor 指标失败：{exc}", file=sys.stderr)
        return 1
    sections = build_return_performance_sections(results)
    skipped_groups = []
    report_date = report_date_china()
    payload = {
        "msg_type": "interactive",
        "card": build_return_performance_card(sections, report_date),
    }

    anomaly_counts = ", ".join(
        f"{section['period']}={len(section['rows'])}" for section in sections
    )
    print(
        f"数据日期={report_date}，配置账户={configured_account_count}，"
        f"可计算账户={len(results)}，展示记录：{anomaly_counts}",
        file=sys.stderr,
    )
    if skipped_accounts:
        print(f"跳过账户={len(skipped_accounts)}", file=sys.stderr)
        for message in skipped_accounts[:10]:
            print(f"  - {message}", file=sys.stderr)
        if len(skipped_accounts) > 10:
            print(f"  - 其余 {len(skipped_accounts) - 10} 个略", file=sys.stderr)
    if skipped_groups:
        print(f"跳过分组周期={len(skipped_groups)}", file=sys.stderr)
        for message in skipped_groups:
            print(f"  - {message}", file=sys.stderr)

    if not args.send:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(
            "\n真实数据预览完成；确认目标后使用 --send。",
            file=sys.stderr,
        )
        return 0

    webhook_url = FEISHU_TEST_BOT_WEBHOOK_URL.strip()
    if not webhook_url:
        print(
            "拒绝发送：请先填写脚本顶部的 FEISHU_TEST_BOT_WEBHOOK_URL。",
            file=sys.stderr,
        )
        return 2
    if not webhook_url.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/"):
        print("拒绝发送：测试群 Webhook 地址格式不符合预期。", file=sys.stderr)
        return 2

    try:
        send(webhook_url, payload)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("真实数据卡片已发送到 FEISHU_TEST_BOT_WEBHOOK_URL 指定的测试群。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
