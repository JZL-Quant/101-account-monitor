import unittest

from core.feishu.router import FeishuRouteError, FeishuRouter


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def send_payload(self, payload, webhook_urls, retries=5):
        self.calls.append((payload, tuple(webhook_urls), retries))

    async def close(self):
        pass


class FeishuRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_none_and_empty_routes_fall_back_to_default(self):
        client = RecordingClient()
        router = FeishuRouter(
            {"default": ["default-1", "default-2"], "empty": [], "none": None},
            client=client,
        )

        for route in ("missing", "empty", "none"):
            await router.send_card({"route": route}, route=route)

        self.assertEqual(
            [call[1] for call in client.calls],
            [("default-1", "default-2")] * 3,
        )

    async def test_configured_route_is_used(self):
        client = RecordingClient()
        router = FeishuRouter(
            {"default": ["default"], "daily_report": ["daily"]}, client=client
        )

        await router.send_card({}, route="daily_report")

        self.assertEqual(client.calls[0][1], ("daily",))

    async def test_send_card_accepts_multiple_cards_with_one_explicit_route(self):
        client = RecordingClient()
        router = FeishuRouter(
            {"default": ["default"], "daily_report": ["daily"]}, client=client
        )

        await router.send_card([{"id": 1}, {"id": 2}], route="daily_report")

        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call[1] == ("daily",) for call in client.calls))
        self.assertEqual(
            {call[0]["card"]["id"] for call in client.calls}, {1, 2}
        )

    async def test_send_card_rejects_unsupported_input(self):
        router = FeishuRouter({"default": ["default"]}, client=RecordingClient())

        with self.assertRaises(TypeError):
            await router.send_card("not-a-card")

    async def test_test_route_never_falls_back(self):
        router = FeishuRouter({"default": ["production"], "test": []}, client=RecordingClient())

        with self.assertRaises(FeishuRouteError):
            await router.send_card({}, route="test")

        with self.assertRaises(FeishuRouteError):
            await router.send_card([{"id": 1}], route="test")


if __name__ == "__main__":
    unittest.main()
