import asyncio


class FeishuRouteError(ValueError):
    pass


def _normalize_urls(value):
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(url.strip() for url in value if isinstance(url, str) and url.strip()))


class FeishuRouter:
    """按任务选择 webhook；普通路由为空时静默回落到 default。"""

    def __init__(self, routes=None, client=None):
        self.routes = {
            str(name): _normalize_urls(urls)
            for name, urls in (routes or {}).items()
            if isinstance(name, str) and name.strip()
        }
        if client is None:
            from core.feishu.client import FeishuClient

            client = FeishuClient()
        self.client = client

    def resolve(self, route=None):
        default_urls = self.routes.get("default", ())
        if route in (None, "", "default"):
            return default_urls
        route_urls = self.routes.get(route, ())
        if route_urls:
            return route_urls
        if route == "test":
            raise FeishuRouteError("Feishu test route is missing or empty; refusing default fallback")
        return default_urls

    async def send_payload(self, payload, route=None, retries=5):
        return await self.client.send_payload(payload, self.resolve(route), retries=retries)

    async def send_message(self, text, route=None, retries=5):
        return await self.send_payload(
            {"msg_type": "text", "content": {"text": text}}, route=route, retries=retries
        )

    async def send_card(self, card_or_cards, route=None, retries=5):
        """发送单张卡片，或并发发送列表/元组中的多张卡片。"""
        if isinstance(card_or_cards, dict):
            return await self.send_payload(
                {"msg_type": "interactive", "card": card_or_cards},
                route=route,
                retries=retries,
            )
        if not isinstance(card_or_cards, (list, tuple)):
            raise TypeError("card_or_cards must be a card dict, list, or tuple")
        if not card_or_cards:
            return []
        return await asyncio.gather(*(
            self.send_card(card, route=route, retries=retries)
            for card in card_or_cards
        ))

    async def upload_image(self, image_path):
        return await self.client.upload_image(image_path)

    async def close(self):
        return await self.client.close()
