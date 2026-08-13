from datetime import date


RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _format_equity_usdt(value):
    return f"{value / 10_000:.0f}"


def _ranking_table(group):
    rows = group["rows"]
    return {
        "tag": "table",
        "element_id": f"weekly_client_ranking_{group['key']}",
        "page_size": max(1, min(len(rows), 10)),
        "row_height": "middle",
        "header_style": {
            "background_style": "none",
            "bold": True,
            "text_align": "center",
            "lines": 1,
        },
        "columns": [
            {"name": "rank", "display_name": "排名", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "80px"},
            {"name": "customer", "display_name": "客户", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "120px"},
            {"name": "return", "display_name": "7D加权年化", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "120px"},
            {"name": "equity", "display_name": "有效权益(wU)", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "120px"},
            {"name": "change", "display_name": "较上周", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "90px"},
            {"name": "accounts", "display_name": "有效账户", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "90px"},
        ],
        "rows": [
            {
                "rank": RANK_MEDALS.get(row["rank"], str(row["rank"])),
                "customer": row["client"],
                "return": f"{row['annualized_return_7d']:+.2f}%",
                "equity": _format_equity_usdt(row["total_equity_usdt"]),
                "accounts": f"{row['valid_account_count']}/{row['account_count']}",
                "change": _format_rank_change(row.get("rank_change")),
            }
            for row in rows
        ],
    }


def _format_rank_change(change):
    if change is None:
        return "新上榜"
    if change > 0:
        return f"↑{change}"
    if change < 0:
        return f"↓{abs(change)}"
    return "—"


def build_weekly_client_ranking_card(ranking):
    report_date = ranking.report_date or date.today()
    date_text = report_date.strftime("%Y-%m-%d")
    elements = [{
        "tag": "markdown",
        "content": "BTC 折 U，收益按权益加权。",
    }]
    for group in ranking.groups:
        elements.append(_ranking_table(group))
    if ranking.skipped_accounts:
        elements.append({
            "tag": "markdown",
            "content": f"<font color='grey'>{len(ranking.skipped_accounts)} 个账户未满 7 天，未参与排名。</font>",
        })
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "每周客户收益排名"},
            "subtitle": {"tag": "plain_text", "content": date_text},
            "template": "blue",
            "icon": {"tag": "standard_icon", "token": "trophy"},
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
