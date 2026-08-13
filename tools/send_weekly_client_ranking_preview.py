#!/usr/bin/env python3
"""通过正式业务入口向严格 test 路由发送客户周排名。"""

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from account_monitor import NOTIFIER, update_weekly_client_ranking


async def _main():
    try:
        ranking = await update_weekly_client_ranking(
            route="test",
            require_route=True,
            persist_history=False,
        )
        if ranking is None:
            raise RuntimeError("没有有效客户排名数据，未发送")
        client_count = sum(len(group["rows"]) for group in ranking.groups)
        print(f"测试群发送完成：客户={client_count}，跳过账户={len(ranking.skipped_accounts)}")
    finally:
        await NOTIFIER.close()


if __name__ == "__main__":
    asyncio.run(_main())
