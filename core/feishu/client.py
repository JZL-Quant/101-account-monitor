import asyncio
import os
import random

import aiohttp

from core.runtime_logging import setup_runtime_logger


RUNTIME_LOGGER = setup_runtime_logger("feishu")


class FeishuClient:
    """飞书 HTTP 客户端；发送目标由上层路由显式传入。"""

    DEFAULT_CARD_TEMPLATE_ID = "AAqNp4fBl3x4y"

    def __init__(self, app_id=None, app_secret=None, max_concurrency=2):
        self.app_id = app_id
        self.app_secret = app_secret
        self._max_concurrency = max_concurrency
        self._loop = None
        self._session = None
        self._session_lock = None
        self._send_semaphore = None

    async def _ensure_loop_resources(self):
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return

        old_session = self._session
        self._loop = loop
        self._session = None
        self._session_lock = asyncio.Lock()
        self._send_semaphore = asyncio.Semaphore(self._max_concurrency)
        if old_session is not None and not old_session.closed:
            await old_session.close()

    async def _get_session(self):
        await self._ensure_loop_resources()
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._loop = None
        self._session = None
        self._session_lock = None
        self._send_semaphore = None

    async def send_payload(self, json_data: dict, webhook_urls, retries: int = 5):
        targets = tuple(dict.fromkeys(url for url in webhook_urls if url))
        if not targets:
            RUNTIME_LOGGER.warning("no Feishu webhook configured; payload skipped")
            return

        headers = {"Content-Type": "application/json"}
        await self._ensure_loop_resources()
        async with self._send_semaphore:
            session = await self._get_session()
            for url in targets:
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
                                success = True
                                break
                            RUNTIME_LOGGER.warning(
                                "attempt %s failed: status=%s, code=%s, msg=%s",
                                attempt, resp.status, code, msg,
                            )
                    except Exception:
                        RUNTIME_LOGGER.exception("attempt %s exception (%s)", attempt, url[:20])
                    if attempt < retries:
                        await asyncio.sleep((2 ** (attempt - 1)) + random.uniform(0, 0.5))
                if not success:
                    RUNTIME_LOGGER.error("all attempts failed for %s", url)

    async def get_tenant_access_token(self):
        if not self.app_id or not self.app_secret:
            RUNTIME_LOGGER.warning("app id/secret not configured; image upload disabled")
            return None
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            session = await self._get_session()
            async with session.post(
                url,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            ) as resp:
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
        headers = {"Authorization": f"Bearer {token}"}
        try:
            form = aiohttp.FormData()
            form.add_field("image_type", "message")
            with open(image_path, "rb") as image_file:
                form.add_field(
                    "image", image_file, filename=os.path.basename(image_path), content_type="image/png"
                )
                session = await self._get_session()
                async with session.post(
                    "https://open.feishu.cn/open-apis/im/v1/images",
                    headers=headers,
                    data=form,
                    timeout=20,
                ) as resp:
                    resp_json = await resp.json()
                    if resp.status == 200 and resp_json.get("code") == 0:
                        return resp_json.get("data", {}).get("image_key")
                    RUNTIME_LOGGER.warning("image upload failed: status=%s, resp=%s", resp.status, resp_json)
        except Exception:
            RUNTIME_LOGGER.exception("image upload exception")
        return None
