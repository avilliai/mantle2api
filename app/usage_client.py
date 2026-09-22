# app/usage_client.py

import json
import logging
from typing import Any, Dict, Optional, Tuple
from curl_cffi.requests import AsyncSession

from app.config import MANTLE_API, HTTP_PROXY
from app.models import AccountState

log = logging.getLogger("mantle_proxy.usage_client")
IMPERSONATE = "chrome120"


async def fetch_mantle_usage(
    account: AccountState, timeout: int = 15
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    通过 https://mantle.chat/api/subscriptions/usage-status?workspaceId=... 探测账号配额与存活状态

    返回值:
        (alive, usage_data, error_message)
        - alive 为 True 表示 session 存活有效
        - usage_data 包含:
          {
              "total": 1500,
              "used": 17,
              "available": 1483,
              "percentage": 1.13,
              "planType": "free",
              "periodStartedAt": "...",
              "resetsAt": "...",
              "resetsInSeconds": ...
          }
        - alive 为 False 表示 session 已失效或请求失败
    """
    ws = account.workspace_id
    if not ws:
        return False, None, f"账号 {account.email} 缺少 workspace_id"

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "x-workspace-id": ws,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    url = f"{MANTLE_API}/subscriptions/usage-status?workspaceId={ws}"

    try:
        async with AsyncSession(proxy=HTTP_PROXY or None, impersonate=IMPERSONATE) as session:
            for k, v in account.cookies.items():
                session.cookies.set(k, v, domain="mantle.chat")

            resp = await session.get(url, headers=headers, timeout=timeout)

        if resp.status_code == 200:
            data = resp.json()
            included = data.get("included", {})
            total = int(included.get("total", 1500))
            used = int(included.get("used", 0))
            available = int(included.get("available", total - used))
            percentage = float(included.get("percentage", 0.0))

            usage_info = {
                "total": total,
                "used": used,
                "available": available,
                "percentage": percentage,
                "planType": data.get("planType", "free"),
                "periodStartedAt": included.get("periodStartedAt"),
                "resetsAt": included.get("resetsAt"),
                "resetsInSeconds": included.get("resetsInSeconds"),
            }
            return True, usage_info, None
        elif resp.status_code in (401, 403):
            return False, None, f"Session 过期或未授权 (HTTP {resp.status_code})"
        else:
            return False, None, f"HTTP {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return False, None, str(e)