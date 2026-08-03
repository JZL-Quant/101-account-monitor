import math


def safe_metric_val(val):
    try:
        if val is None:
            return None
        float_val = float(val)
        if math.isnan(float_val) or math.isinf(float_val):
            return None
        return float_val
    except Exception:
        return None

def fmt_compact_ccy(val, ccy):
    safe_val = safe_metric_val(val)
    if safe_val is None:
        return "N/A"
    if ccy == "USDT" and abs(safe_val) >= 10000:
        return f"{safe_val / 10000:,.2f}万 {ccy}"
    return f"{safe_val:,.4f} {ccy}" if ccy == "BTC" else f"{safe_val:,.2f} {ccy}"

def metric_color(val, positive_color="green", negative_color="red", zero_color="grey"):
    safe_val = safe_metric_val(val)
    if safe_val is None:
        return "grey"
    if safe_val > 0:
        return positive_color
    if safe_val < 0:
        return negative_color
    return zero_color

def md_pct_plain_colored(val, color=None):
    safe_val = safe_metric_val(val)
    if safe_val is None:
        return "<font color='grey'>N/A</font>"
    if color is None:
        color = metric_color(safe_val)
    sign = "+" if safe_val > 0 else ""
    return f"<font color='{color}'>{sign}{safe_val:.2f}%</font>"

def card2_markdown(content, **kwargs):
    element = {
        "tag": "markdown",
        "content": content
    }
    element.update(kwargs)
    return element

def _metric_column(value_content, label_content):
    return {
        "tag": "column",
        "width": "weighted",
        "elements": [
            card2_markdown(value_content, text_align="center", text_size="normal"),
            card2_markdown(label_content, text_align="center", text_size="normal")
        ],
        "vertical_spacing": "8px",
        "horizontal_align": "left",
        "vertical_align": "top",
        "weight": 1
    }

def _metric_pair_column(left_value, left_label, right_value, right_label, background_style, margin):
    return {
        "tag": "column",
        "width": "weighted",
        "background_style": background_style,
        "elements": [
            {
                "tag": "column_set",
                "horizontal_spacing": "8px",
                "horizontal_align": "left",
                "columns": [
                    _metric_column(left_value, left_label),
                    _metric_column(right_value, right_label)
                ],
                "margin": "0px 0px 0px 0px"
            }
        ],
        "padding": "8px 8px 8px 8px",
        "direction": "vertical",
        "horizontal_spacing": "8px",
        "vertical_spacing": "2px",
        "horizontal_align": "left",
        "vertical_align": "top",
        "margin": margin,
        "weight": 1
    }

def _chart_value(val):
    safe_val = safe_metric_val(val)
    return 0 if safe_val is None else round(safe_val, 2)

def _summary_size_element(total_equity, benchmark, ccy):
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "12px",
        "horizontal_align": "left",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "background_style": "blue-50",
                "elements": [
                    {
                        "tag": "column_set",
                        "horizontal_spacing": "8px",
                        "horizontal_align": "left",
                        "columns": [
                            _metric_column(
                                f"# <font color='blue'>{fmt_compact_ccy(total_equity, ccy)}</font>",
                                "<font color='grey'>组合规模</font>"
                            ),
                            _metric_column(
                                f"# <font color='green'>{benchmark:g}%</font>",
                                "<font color='grey'>资金成本</font>"
                            )
                        ],
                        "margin": "0px 0px 0px 0px"
                    }
                ],
                "padding": "8px 6px 8px 6px",
                "direction": "horizontal",
                "horizontal_spacing": "8px",
                "vertical_spacing": "2px",
                "horizontal_align": "left",
                "vertical_align": "top",
                "margin": "4px 2px 0px 2px",
                "weight": 1
            }
        ],
        "margin": "0px 0px 0px 0px"
    }

def _summary_return_element(combined, combined_excess):
    excess_color = "green" if (safe_metric_val(combined_excess) or 0) >= 0 else "red"
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "8px",
        "horizontal_align": "left",
        "columns": [
            _metric_pair_column(
                f"# {md_pct_plain_colored(combined.get('ar24h'), 'purple')}",
                "<font color='grey'>当日收益</font>",
                f"# {md_pct_plain_colored(combined.get('ar7d'), 'purple')}",
                "<font color='grey'>7D收益</font>",
                "purple-50",
                "0px 0px 0px 2px"
            ),
            _metric_pair_column(
                f"# {md_pct_plain_colored(combined.get('ar30d'), 'purple')}",
                "<font color='grey'>30D收益</font>",
                f"# {md_pct_plain_colored(combined_excess, excess_color)}",
                "<font color='grey'>超额收益</font>",
                "violet-50",
                "0px 2px 0px 0px"
            )
        ],
        "margin": "0px 0px 0px 0px"
    }

def _returns_chart_element(chart_values):
    return {
        "tag": "chart",
        "chart_spec": {
            "data": {
                "values": chart_values
            },
            "legends": {
                "orient": "bottom",
                "visible": True
            },
            "seriesField": "metric",
            "title": {
                "text": "账号年化收益"
            },
            "type": "bar",
            "label": {
                "visible": True,
                "formatter": "{value}%"
            },
            "axes": [
                {
                    "orient": "left",
                    "label": {
                        "visible": False,
                        "formatter": "{value}%"
                    },
                    "title": {
                        "visible": True,
                        "text": "年化收益(%)"
                    }
                },
                {
                    "orient": "bottom"
                }
            ],
            "xField": [
                "account",
                "metric"
            ],
            "yField": "value"
        },
        "preview": True,
        "color_theme": "brand",
        "height": "auto",
        "margin": "0px 0px 0px 0px"
    }

def _returns_table_element(table_columns, table_rows):
    return {
        "tag": "table",
        "columns": table_columns,
        "rows": table_rows,
        "row_height": "middle",
        "header_style": {
            "text_align": "center",
            "background_style": "none",
            "bold": True,
            "lines": 1
        },
        "page_size": max(5, min(len(table_rows), 20)),
        "margin": "0px 0px 0px 0px",
        "element_id": "account_return_table"
    }

def _card_body(elements):
    return {
        "direction": "vertical",
        "horizontal_spacing": "8px",
        "vertical_spacing": "8px",
        "horizontal_align": "left",
        "vertical_align": "top",
        "padding": "4px 4px 4px 4px",
        "elements": elements
    }

def _card_header(title, today_str):
    return {
        "title": {"tag": "plain_text", "content": f"{title} 组合收益分析报告"},
        "subtitle": {"tag": "plain_text", "content": today_str},
        "template": "blue",
        "icon": {"tag": "standard_icon", "token": "chart-bar"},
        "padding": "12px 16px 12px 16px"
    }

def build_group_schema2_card(cfg, sorted_results, today_str, combined, chart_image_key=None):
    title = cfg["title"]
    benchmark = cfg["benchmark"]
    ccy = cfg.get("ccy", "BTC")
    total_equity = sum(
        safe_metric_val(res.get("actual_equity")) or 0.0
        for res in sorted_results
    )

    safe_c_ar24h = safe_metric_val(combined.get("ar24h"))
    combined_excess = safe_c_ar24h - benchmark if safe_c_ar24h is not None else None

    chart_values = []
    table_columns = [
        {
            "data_type": "markdown",
            "name": "account_name",
            "display_name": "账户",
            "horizontal_align": "center",
            "vertical_align": "center",
            "width": "120px"
        },
        {
            "data_type": "markdown",
            "name": "daily_return",
            "display_name": "当日年化",
            "horizontal_align": "center",
            "vertical_align": "center",
            "width": "110px"
        },
        {
            "data_type": "markdown",
            "name": "7d_return",
            "display_name": "7D年化",
            "horizontal_align": "center",
            "vertical_align": "center",
            "width": "110px"
        },
        {
            "data_type": "markdown",
            "name": "30d_return",
            "display_name": "30D年化",
            "horizontal_align": "center",
            "vertical_align": "center",
            "width": "110px"
        },
        {
            "data_type": "markdown",
            "name": "excess_return",
            "display_name": "超额收益",
            "horizontal_align": "center",
            "vertical_align": "center",
            "width": "110px"
        }
    ]

    table_rows = []

    for res in sorted_results:
        account_name = res["display_name"]
        safe_ar24h = safe_metric_val(res.get("ar24h"))
        excess = safe_ar24h - benchmark if safe_ar24h is not None else None

        chart_values.extend([
            {"account": account_name, "metric": "当日", "value": _chart_value(res.get("ar24h"))},
            {"account": account_name, "metric": "7D", "value": _chart_value(res.get("ar7d"))},
            {"account": account_name, "metric": "30D", "value": _chart_value(res.get("ar30d"))}
        ])

        table_rows.append({
            "account_name": account_name,
            "daily_return": md_pct_plain_colored(res.get("ar24h"), "blue"),
            "7d_return": md_pct_plain_colored(res.get("ar7d"), "orange"),
            "30d_return": md_pct_plain_colored(res.get("ar30d"), "purple"),
            "excess_return": md_pct_plain_colored(excess)
        })

    elements = [
        _summary_size_element(total_equity, benchmark, ccy),
        _summary_return_element(combined, combined_excess),
        card2_markdown("### <font color='blue'>组内账号年化收益分布</font>", text_size="normal", margin="0px 0px 0px 0px"),
        _returns_chart_element(chart_values),
        card2_markdown("### <font color='blue'>账号收益明细</font>", text_size="normal", margin="0px 0px 0px 0px"),
        _returns_table_element(table_columns, table_rows)
    ]

    return {
        "schema": "2.0",
        "config": {
            "update_multi": True
        },
        "body": _card_body(elements),
        "header": _card_header(title, today_str)
    }


def build_daily_detail_cards(report):
    """把日报中的各账户组转换为飞书明细卡片。"""
    report_date = report.report_date.strftime("%Y-%m-%d")
    return [
        build_group_schema2_card(
            item["group"], item["results"], report_date, item["combined"]
        )
        for item in report.detail_groups
    ]

# ---------- 加载 binance_config.yaml 配置 ----------
