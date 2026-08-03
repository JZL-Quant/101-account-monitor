#!/usr/bin/env python3
"""Build a real-data return anomaly card and optionally send it to a test group."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# 只填写测试群机器人的 Webhook；不要填写正式群地址。
FEISHU_TEST_BOT_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/af6b4e9f-9981-4c78-bc18-9b1bccc3e620"
ACCOUNT_MONITOR_BASE_URL = "http://127.0.0.1:7007"

MIN_VALID_GROUP_SIZE = 5
MIN_VALID_STANDARD_DEVIATION = 1e-7
ATTENTION_Z_THRESHOLD = -1.5
PRIORITY_ATTENTION_Z_THRESHOLD = -2.0
OUTPERFORM_Z_THRESHOLD = 2.5
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
                "account": account_name,
                "exchange_id": exchange.lower(),
                "exchange_label": exchange,
                "ccy": ccy,
                **returns,
            }
        )
        if missing_metrics:
            skipped.append(f"{account_name}: 缺少 {', '.join(missing_metrics)}")

    return results, skipped, len(accounts)


def build_real_sections(results: list[dict]) -> tuple[list[tuple], list[str]]:
    """Calculate same-exchange/same-ccy population Z scores for each period."""
    sections = []
    skipped_groups = []

    for title, element_id, metric_key, _ in PERIODS:
        grouped: dict[tuple[str, str], list[dict]] = {}
        for result in results:
            value = result.get(metric_key)
            if value is None or not math.isfinite(float(value)):
                continue
            group_key = (result["exchange_id"], result["ccy"])
            grouped.setdefault(group_key, []).append(result)

        flagged = []
        for (_, ccy), group_results in grouped.items():
            exchange_label = str(group_results[0]["exchange_label"])
            group_label = f"{exchange_label}-{ccy}"
            if len(group_results) < MIN_VALID_GROUP_SIZE:
                skipped_groups.append(
                    f"{title}/{group_label}: 有效账户 {len(group_results)} 个，少于 {MIN_VALID_GROUP_SIZE} 个"
                )
                continue

            values = [float(result[metric_key]) for result in group_results]
            group_mean = statistics.fmean(values)
            group_std = statistics.pstdev(values)
            if not math.isfinite(group_std) or group_std <= MIN_VALID_STANDARD_DEVIATION:
                skipped_groups.append(f"{title}/{group_label}: 标准差为 0")
                continue

            for result in group_results:
                value = float(result[metric_key])
                z_score = (value - group_mean) / group_std
                if z_score <= ATTENTION_Z_THRESHOLD or z_score >= OUTPERFORM_Z_THRESHOLD:
                    flagged.append(
                        (
                            z_score,
                            (
                                group_label,
                                result["account"],
                                f"{value:+.2f}%",
                                f"{group_mean:+.2f}%",
                                f"{group_std:.2f}%",
                                f"{z_score:.2f}",
                            ),
                        )
                    )

        rows = [row for _, row in sorted(flagged, key=lambda item: item[0])]
        sections.append((title, element_id, rows))

    return sections, skipped_groups


def markdown(content: str) -> dict:
    return {"tag": "markdown", "content": content}


def anomaly_table(element_id: str, rows: list[tuple[str, str, str, str, str, str]]) -> dict:
    table_rows = []
    for group, account, return_value, mean, std_dev, z_score in rows:
        numeric_z = float(z_score)
        if numeric_z >= OUTPERFORM_Z_THRESHOLD:
            marker = "🟢"
        elif numeric_z <= PRIORITY_ATTENTION_Z_THRESHOLD:
            marker = "🟠"
        else:
            marker = "🟡"
        table_rows.append(
            {
                "group": group,
                "account": f"{marker} {account}",
                "return": return_value,
                "mean": mean,
                "std_dev": std_dev,
                "z_score": z_score,
            }
        )

    return {
        "tag": "table",
        "element_id": element_id,
        "page_size": min(max(len(rows), 1), 10),
        "row_height": "middle",
        "header_style": {
            "background_style": "none",
            "bold": True,
            "text_align": "center",
            "lines": 1,
        },
        "columns": [
            {
                "name": "account",
                "display_name": "账户",
                "data_type": "text",
                "horizontal_align": "center",
                "vertical_align": "center",
                "width": "120px",
            },
            {
                "name": "return",
                "display_name": "收益率",
                "data_type": "text",
                "horizontal_align": "center",
                "vertical_align": "center",
                "width": "90px",
            },
            {
                "name": "mean",
                "display_name": "同组均值",
                "data_type": "text",
                "horizontal_align": "center",
                "vertical_align": "center",
                "width": "90px",
            },
            {
                "name": "std_dev",
                "display_name": "标准差",
                "data_type": "text",
                "horizontal_align": "center",
                "vertical_align": "center",
                "width": "90px",
            },
            {
                "name": "z_score",
                "display_name": "Z 值",
                "data_type": "text",
                "horizontal_align": "center",
                "vertical_align": "center",
                "width": "80px",
            },
            {
                "name": "group",
                "display_name": "交易所-本位",
                "data_type": "text",
                "horizontal_align": "center",
                "vertical_align": "center",
                "width": "120px",
            },
        ],
        "rows": table_rows,
    }


def build_card(sections: list[tuple], report_date) -> dict:
    elements = [
        markdown(
            "**Z值表示偏离同组均值多少个标准差。**\n"
            f"<font color='green'>**表现突出**</font>：Z ≥ {OUTPERFORM_Z_THRESHOLD:g}，"
            f"<font color='orange'>**关注**</font>：{PRIORITY_ATTENTION_Z_THRESHOLD:g} < Z ≤ {ATTENTION_Z_THRESHOLD:g}，"
            f"<font color='red'>**重点关注**</font>：Z ≤ {PRIORITY_ATTENTION_Z_THRESHOLD:g}。"
        )
    ]
    for title, element_id, rows in sections:
        elements.append(markdown(f"### <font color='blue'>{title}</font>"))
        elements.append(anomaly_table(element_id, rows))

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "收益率账户表现总览"},
            "subtitle": {"tag": "plain_text", "content": report_date.strftime("%Y-%m-%d")},
            "template": "blue",
            "icon": {"tag": "standard_icon", "token": "chart-bar"},
            "padding": "12px 16px 12px 16px",
        },
        "body": {
            "direction": "vertical",
            "horizontal_spacing": "8px",
            "vertical_spacing": "8px",
            "horizontal_align": "left",
            "vertical_align": "top",
            "padding": "4px 4px 4px 4px",
            "elements": elements,
        },
    }


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
    sections, skipped_groups = build_real_sections(results)
    report_date = report_date_china()
    payload = {
        "msg_type": "interactive",
        "card": build_card(sections, report_date),
    }

    anomaly_counts = ", ".join(
        f"{title.split()[0]}={len(rows)}" for title, _, rows in sections
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
