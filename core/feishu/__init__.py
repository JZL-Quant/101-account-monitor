"""飞书发送、路由和卡片模块。网络客户端按需加载。"""

__all__ = ["FeishuClient", "FeishuRouteError", "FeishuRouter", "NOTIFIER"]


def __getattr__(name):
    if name == "FeishuClient":
        from core.feishu.client import FeishuClient

        return FeishuClient
    if name in {"FeishuRouteError", "FeishuRouter"}:
        from core.feishu.router import FeishuRouteError, FeishuRouter

        return {"FeishuRouteError": FeishuRouteError, "FeishuRouter": FeishuRouter}[name]
    if name == "NOTIFIER":
        from config.settings import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_WEBHOOK_ROUTES
        from core.feishu.client import FeishuClient
        from core.feishu.router import FeishuRouter

        notifier = FeishuRouter(
            routes=FEISHU_WEBHOOK_ROUTES,
            client=FeishuClient(app_id=FEISHU_APP_ID, app_secret=FEISHU_APP_SECRET),
        )
        globals()["NOTIFIER"] = notifier
        return notifier
    raise AttributeError(name)
