"""本地敏感配置模板。

复制为 ``local_secrets.py`` 后填写真实值。不要在此示例文件中填写密钥。
"""

FEISHU_BOT_WEBHOOK_URLS = [
    # "https://open.feishu.cn/open-apis/bot/v2/hook/群机器人-webhook-1",
    # "https://open.feishu.cn/open-apis/bot/v2/hook/群机器人-webhook-2",
]
# 可选：任务专用路由。缺失、None 或空列表时，普通路由回退到 default。
# test 路由是严格路由，缺失或为空时拒绝发送，不会回退正式群。
FEISHU_WEBHOOK_ROUTES = {
    "default": FEISHU_BOT_WEBHOOK_URLS,
    # "daily_report": [],
    # "return_performance": [],
    # "equity_change_alert": [],
    # "test": ["https://open.feishu.cn/open-apis/bot/v2/hook/测试群-webhook"],
}
FEISHU_APP_ID = ""
FEISHU_APP_SECRET = ""

GRAFANA_API_TOKEN = ""
GRAFANA_USER = ""
GRAFANA_PASSWORD = ""

ANNUALIZED_DASHBOARD_UID = ""
NAV_DASHBOARD_UID = ""
REFERENCE_ANNUALIZED_DASHBOARD_UID = ""
REFERENCE_NAV_DASHBOARD_UID = ""
