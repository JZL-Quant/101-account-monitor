#!/usr/bin/env python3
"""Send a demo anomaly-summary card to an explicitly configured test group."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


TEST_WEBHOOK_ENV = "FEISHU_TEST_BOT_WEBHOOK_URL"


DEMO_SECTIONS = (
    (
        "24h 收益率异常",
        "anomaly_24h_table",
        [
            ("Binance-USDT", "示例账户_A", "-18.42%", "4.85%", "-2.36"),
            ("Gate-USDT", "示例账户_B", "-9.16%", "3.21%", "-2.08"),
        ],
    ),
    (
        "7D 收益率异常",
        "anomaly_7d_table",
        [
            ("Binance-BTC", "示例账户_C", "1.72%", "8.64%", "-2.21"),
        ],
    ),
    (
        "30D 收益率异常",
        "anomaly_30d_table",
        [
            ("Gate-BTC", "示例账户_D", "2.35%", "9.48%", "-2.14"),
            ("Binance-USDT", "示例账户_E", "3.06%", "10.12%", "-2.03"),
        ],
    ),
)


def markdown(content: str) -> dict:
    return {"tag": "markdown", "content": content}


def anomaly_table(element_id: str, rows: list[tuple[str, str, str, str, str]]) -> dict:
    return {
        "tag": "table",
        "element_id": element_id,
        "page_size": min(max(len(rows), 1), 10),
        "row_height": "middle",
        "header_style": {
            "background_style": "red-50",
            "bold": True,
            "text_align": "center",
            "lines": 1,
        },
        "columns": [
            {
                "name": "group",
                "display_name": "交易所-本位",
                "data_type": "text",
                "width": "120px",
            },
            {
                "name": "account",
                "display_name": "账户",
                "data_type": "text",
                "width": "120px",
            },
            {
                "name": "return",
                "display_name": "收益率",
                "data_type": "markdown",
                "horizontal_align": "right",
                "width": "90px",
            },
            {
                "name": "mean",
                "display_name": "同组均值",
                "data_type": "text",
                "horizontal_align": "right",
                "width": "90px",
            },
            {
                "name": "z_score",
                "display_name": "Z 值",
                "data_type": "markdown",
                "horizontal_align": "right",
                "width": "70px",
            },
        ],
        "rows": [
            {
                "group": group,
                "account": account,
                "return": f"<font color='red'>**{return_value}**</font>",
                "mean": mean,
                "z_score": f"<font color='red'>**{z_score}**</font>",
            }
            for group, account, return_value, mean, z_score in rows
        ],
    }


def build_demo_card() -> dict:
    elements = [
        markdown(
            "仅展示 **Z ≤ -2** 的账户；按 **同交易所 + 同本位币** 分组计算。"
            "以下均为样式演示数据。"
        )
    ]
    for title, element_id, rows in DEMO_SECTIONS:
        elements.append(markdown(f"### <font color='red'>{title}</font>"))
        elements.append(anomaly_table(element_id, rows))

    elements.append(
        markdown(
            "<font color='grey'>本次演示：4 个分组，5 条异常记录。"
            "正式数据中，无异常的周期将显示“未发现异常账户”。</font>"
        )
    )
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "收益率异常账户总览｜样式预览"},
            "subtitle": {"tag": "plain_text", "content": "测试群专用 · 演示数据"},
            "template": "red",
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "10px",
            "padding": "12px 12px 12px 12px",
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
        description="预览或向测试飞书群发送三表格异常总览样式卡片。"
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help=f"真正发送；未指定时只输出卡片 JSON。目标仅从 {TEST_WEBHOOK_ENV} 读取。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = {"msg_type": "interactive", "card": build_demo_card()}

    if not args.send:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"\n预览完成；确认目标后使用 --send，并配置 {TEST_WEBHOOK_ENV}。", file=sys.stderr)
        return 0

    webhook_url = os.getenv(TEST_WEBHOOK_ENV, "").strip()
    if not webhook_url:
        print(
            f"拒绝发送：未配置专用测试群环境变量 {TEST_WEBHOOK_ENV}。",
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

    print("样式卡片已发送到 FEISHU_TEST_BOT_WEBHOOK_URL 指定的测试群。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
