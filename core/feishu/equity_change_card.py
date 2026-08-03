def build_large_equity_change_card(changes):
    """构造分钟级大额资金变动飞书告警卡片。"""
    elements = []
    for position, change in enumerate(changes):
        amount = float(change["change"])
        ccy = change["ccy"]
        direction = "资金流入" if amount > 0 else "资金流出"
        elements.append({
            "tag": "markdown",
            "content": (
                f"**账户：** {change['account_name']}\n"
                f"**变动时间：** {change['timestamp']}\n"
                f"**变动方向：** {direction}\n"
                f"**权益变化：** {change['previous_equity']:,.8f} → "
                f"{change['actual_equity']:,.8f} {ccy}\n"
                f"**变动金额：** {amount:+,.8f} {ccy}\n"
                f"**告警阈值：** {change['threshold']:,.8f} {ccy}"
            ),
        })
        if position < len(changes) - 1:
            elements.append({"tag": "hr"})
    elements.extend([
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": "<font color='grey'>系统按分钟快照自动检测，请及时核实并登记资金变动。</font>",
        },
    ])
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⚠️ 大额资金变动警告"},
            "template": "red",
        },
        "body": {"elements": elements},
    }
