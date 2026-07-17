import asyncio
import os
import random

import aiohttp

from .runtime_logging import setup_runtime_logger
from config.settings import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_BOT_WEBHOOK_URLS


RUNTIME_LOGGER = setup_runtime_logger("feishu")


class FeishuClient:
    DEFAULT_CARD_TEMPLATE_ID = "AAqNp4fBl3x4y"

    def __init__(self, webhook_urls=None, app_id=None, app_secret=None, max_concurrency=2):
        self.webhook_urls = list(dict.fromkeys(url for url in (webhook_urls or []) if url))
        self.app_id = app_id
        self.app_secret = app_secret
        self._session = None
        self._session_lock = asyncio.Lock()
        self._send_semaphore = asyncio.Semaphore(max_concurrency)

    async def _get_session(self):
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @classmethod
    def from_env(cls):
        return cls(
            webhook_urls=FEISHU_BOT_WEBHOOK_URLS,
            app_id=FEISHU_APP_ID,
            app_secret=FEISHU_APP_SECRET,
        )

    async def send_payload(self, json_data: dict, retries: int = 5):
        headers = {"Content-Type": "application/json"}

        async with self._send_semaphore:
            session = await self._get_session()
            for url in self.webhook_urls:
                success = False
                RUNTIME_LOGGER.info("sending to %s...", url[:30])

                for attempt in range(1, retries + 1):
                    try:
                        async with session.post(url, headers=headers, json=json_data, timeout=10) as resp:
                            response_text = await resp.text()
                            try:
                                resp_json = await resp.json()
                                code = resp_json.get("code", "N/A")
                                msg = resp_json.get("msg", "no message")
                            except Exception:
                                code = "json_parse_failed"
                                msg = response_text[:100]

                            if resp.status == 200 and code == 0:
                                RUNTIME_LOGGER.info("sent to %s on attempt %s", url[:30], attempt)
                                success = True
                                break

                            RUNTIME_LOGGER.warning(
                                "attempt %s failed: status=%s, code=%s, msg=%s",
                                attempt,
                                resp.status,
                                code,
                                msg,
                            )

                    except Exception:
                        RUNTIME_LOGGER.exception("attempt %s exception (%s)", attempt, url[:20])

                    if attempt < retries:
                        delay = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                        RUNTIME_LOGGER.info(
                            "retrying %s after %.2fs (attempt %s/%s)",
                            url[:20],
                            delay,
                            attempt + 1,
                            retries,
                        )
                        await asyncio.sleep(delay)

                if not success:
                    RUNTIME_LOGGER.error("all attempts failed for %s", url)

    async def send_message(self, text: str, retries: int = 5):
        await self.send_payload({
            "msg_type": "text",
            "content": {"text": text},
        }, retries=retries)

    async def send_card(self, card: dict, retries: int = 5):
        await self.send_payload({
            "msg_type": "interactive",
            "card": card,
        }, retries=retries)

    async def get_tenant_access_token(self):
        if not self.app_id or not self.app_secret:
            RUNTIME_LOGGER.warning("app id/secret not configured; image upload disabled")
            return None

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }

        try:
            session = await self._get_session()
            async with session.post(url, json=payload, timeout=10) as resp:
                resp_json = await resp.json()
                if resp.status == 200 and resp_json.get("code") == 0:
                    return resp_json.get("tenant_access_token")
                RUNTIME_LOGGER.warning("tenant token failed: status=%s, resp=%s", resp.status, resp_json)
        except Exception:
            RUNTIME_LOGGER.exception("tenant token exception")
        return None

    async def upload_image(self, image_path):
        token = await self.get_tenant_access_token()
        if not token:
            return None

        url = "https://open.feishu.cn/open-apis/im/v1/images"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            form = aiohttp.FormData()
            form.add_field("image_type", "message")
            with open(image_path, "rb") as image_file:
                form.add_field(
                    "image",
                    image_file,
                    filename=os.path.basename(image_path),
                    content_type="image/png",
                )
                session = await self._get_session()
                async with session.post(url, headers=headers, data=form, timeout=20) as resp:
                    resp_json = await resp.json()
                    if resp.status == 200 and resp_json.get("code") == 0:
                        return resp_json.get("data", {}).get("image_key")
                    RUNTIME_LOGGER.warning("image upload failed: status=%s, resp=%s", resp.status, resp_json)
        except Exception:
            RUNTIME_LOGGER.exception("image upload exception")
        return None


NOTIFIER = FeishuClient.from_env()
