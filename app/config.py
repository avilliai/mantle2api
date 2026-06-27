"""
全局常量与配置
"""
import os
from pathlib import Path

# ── 文件路径 ──────────────────────────────────────────────
TOKENS_FILE = Path("./cosverse_tokens.json")

# ── 上游地址 ──────────────────────────────────────────────
COSVERSE_WEB = "https://chat.cosverse.ai"
COSVERSE_API = "https://chat.cosverse.ai/api"

# ── 默认 HTTP 请求头 ──────────────────────────────────────
COMMON_HEADERS: dict[str, str] = {
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "sec-ch-ua": '"Microsoft Edge";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
    ),
}

# ── Next.js action hashes 和 deployment_id ────────────────
# 这些值由 Vercel 每次部署时重新生成，不能硬编码。
# 运行时由 app/action_fetcher.py 动态从页面 JS bundle 抓取，
# 失败时才使用 action_fetcher.py 内的 _FALLBACK 兜底值。

# ── 代理（可通过环境变量覆盖） ─────────────────────────────
HTTP_PROXY  = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")

if HTTP_PROXY:
    os.environ["http_proxy"]  = HTTP_PROXY
if HTTPS_PROXY:
    os.environ["https_proxy"] = HTTPS_PROXY