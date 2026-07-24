from __future__ import annotations

import logging

import aiohttp

from .config import FeishuConfig
from .models import HedgeRow


LOGGER = logging.getLogger("kucoin_exposure.feishu")


class FeishuNotifier:
    def __init__(self, config: FeishuConfig):
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if not self.config.enabled:
            return
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def send_exposure_alert(
        self,
        rows: list[HedgeRow],
        sampled_at: str,
    ) -> None:
        if not self.config.enabled or not rows:
            return
        await self.start()
        assert self._session is not None

        details = []
        for row in rows:
            details.append(
                "\n".join(
                    (
                        f"币种：{row.asset}",
                        f"状态：{row.status}",
                        f"现货数量：{row.spot_qty}",
                        f"合约币数量：{row.futures_qty}",
                        f"净敞口：{row.net_qty}",
                        f"净敞口价值：{row.net_value} USDT",
                        f"偏差率：{row.mismatch_percent}%",
                    )
                )
            )
        text = (
            "【KuCoin敞口报警】\n"
            f"数据时间：{sampled_at}\n\n"
            + "\n\n".join(details)
        )
        async with self._session.post(
            self.config.webhook_url,
            json={"msg_type": "text", "content": {"text": text}},
        ) as response:
            body = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"Feishu webhook HTTP {response.status}: {body[:300]}"
                )
            try:
                result = await response.json(content_type=None)
            except ValueError as exc:
                raise RuntimeError(
                    f"Feishu webhook returned invalid JSON: {body[:300]}"
                ) from exc
            code = result.get("code", result.get("StatusCode", 0))
            if code != 0:
                message = result.get("msg", result.get("StatusMessage", "unknown"))
                raise RuntimeError(f"Feishu webhook error {code}: {message}")
        LOGGER.info(
            "Sent Feishu exposure alert for assets: %s",
            ", ".join(row.asset for row in rows),
        )
