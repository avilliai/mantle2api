import json
import logging
from typing import Any, Dict, Optional, Tuple
from curl_cffi.requests import AsyncSession
from app.action_fetcher import fetch_action_config

log = logging.getLogger(__name__)


async def fetch_usage(
    cookie_str: str, timeout: int = 15
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    ?? https://chat.cosverse.ai/usage ????????????

    ???:
        (alive, limits_dict, error_message)
        - alive ? True ?? session ??????????limits_dict ???:
          {"messageLimit": 100, "messageUsage": 0, "claudeLimit": 0, ...}
        - alive ? False ?????session ????????????
    """
    cfg = await fetch_action_config()
    headers = {
        "accept": "text/x-component",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "content-type": "text/plain;charset=UTF-8",
        "next-action": cfg.usage_action,
        "next-router-state-tree": cfg.router_state_usage(),
        "priority": "u=1, i",
        "referer": "https://chat.cosverse.ai/usage",
        "cookie": cookie_str,
    }
    if cfg.deployment_id:
        headers["x-deployment-id"] = cfg.deployment_id

    try:
        async with AsyncSession(impersonate="chrome110") as session:
            resp = await session.post(
                "https://chat.cosverse.ai/usage",
                headers=headers,
                data="[]",
                timeout=timeout,
            )

        if resp.status_code != 200:
            return False, None, f"HTTP {resp.status_code}"

        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("1:"):
                payload = json.loads(line[2:])
                if payload.get("success"):
                    return True, payload.get("limits", {}), None
                else:
                    return False, None, payload.get("error", "Unknown error")

        return False, None, f"Unrecognized response: {resp.text[:100]}"
    except Exception as e:
        return False, None, str(e)
