from datetime import date


LEVEL_EMOJI = {
    "outstanding": "🟢",
    "attention": "🟡",
    "priority_attention": "🟠",
}


def _signed_percent(value):
    return f"{value:+.2f}%"


def _table(element_id, rows):
    return {
        "tag": "table",
        "element_id": element_id,
        "page_size": max(1, min(len(rows), 10)),
        "row_height": "middle",
        "header_style": {
            "background_style": "none",
            "bold": True,
            "text_align": "center",
            "lines": 1,
        },
        "columns": [
            {"name": "account", "display_name": "账户", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "120px"},
            {"name": "return", "display_name": "收益率", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "90px"},
            {"name": "mean", "display_name": "同组均值", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "90px"},
            {"name": "std", "display_name": "标准差", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "90px"},
            {"name": "z", "display_name": "Z 值", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "80px"},
            {"name": "group", "display_name": "交易所-本位", "data_type": "text", "horizontal_align": "center", "vertical_align": "center", "width": "120px"},
        ],
        "rows": [
            {
                "account": f"{LEVEL_EMOJI[row['level']]} {row['account_name']}",
                "return": _signed_percent(row["return_value"]),
                "mean": _signed_percent(row["group_mean"]),
                "std": f"{row['group_std']:.2f}%",
                "z": f"{row['z_value']:.2f}",
                "group": row["group_label"],
            }
            for row in rows
        ],
    }


def build_return_performance_card(sections, report_date=None):
    report_date = report_date or date.today()
    date_text = report_date.strftime("%Y-%m-%d") if hasattr(report_date, "strftime") else str(report_date)
    elements = [
        {
            "tag": "markdown",
            "content": (
                "**Z值表示偏离同组均值多少个标准差。**\n"
                "<font color='green'>**表现突出**</font>：Z ≥ 2.5，"
                "<font color='orange'>**关注**</font>：-2 < Z ≤ -1.5，"
                "<font color='red'>**重点关注**</font>：Z ≤ -2。"
            ),
        }
    ]
    for section in sections:
        elements.append({
            "tag": "markdown",
            "content": f"### <font color='blue'>{section['title']}</font>",
        })
        elements.append(_table(f"return_performance_{section['period'].lower()}", section["rows"]))
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "收益率账户表现总览"},
            "subtitle": {"tag": "plain_text", "content": date_text},
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
