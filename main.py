# app/main.py

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.routers import admin, chat
from app.routers import model_list as models
from app.cookie_manager import cm

# ─────────────────────────── 日志 ────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ─────────────────────────── 鉴权配置 ────────────────────
API_KEY = os.getenv("PROXY_API_KEY", "endlesswork")
if not API_KEY:
    logging.warning("⚠️  未设置 PROXY_API_KEY，所有请求将被拒绝")

_PUBLIC_PATHS = {"/", "/health"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Missing Authorization header", "type": "auth_error"}},
            )
        token = auth_header[len("Bearer "):]
        if not API_KEY or token != API_KEY:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid API key", "type": "auth_error"}},
            )
        return await call_next(request)


# ─────────────────────────── 动态账号池维护任务 ────────────
# 监控间隔：每分钟扫描一次用量耗尽状况，按需静默激活账号
POOL_MAINTAIN_INTERVAL = int(os.getenv("POOL_MAINTAIN_INTERVAL", "60"))

_pool_log = logging.getLogger("cosverse_proxy.pool_monitor")


async def pool_monitor_task():
    """
    轻量化的动态后台任务，仅看水位，不执行主动探活。
    """
    _pool_log.info(f"⏳ 动态水位监测任务已启动，检查间隔: {POOL_MAINTAIN_INTERVAL}s")
    # 刚启动时立即检查一遍，确保初始目标 10 个号
    await cm.maintain_pool()

    while True:
        try:
            await asyncio.sleep(POOL_MAINTAIN_INTERVAL)
            await cm.maintain_pool()
        except asyncio.CancelledError:
            _pool_log.info("水位监测任务已取消")
            break
        except Exception as e:
            _pool_log.error(f"水位监测任务异常: {e}", exc_info=True)


# ─────────────────────────── 生命周期 ────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    cm.load()
    from app.action_fetcher import fetch_action_config
    await fetch_action_config()
    logging.getLogger("cosverse_proxy").info(
        f"🚀 代理服务启动，已加载本地 {len(cm.accounts)} 个历史账号"
    )

    bg_task = asyncio.create_task(pool_monitor_task())

    yield

    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass


# ─────────────────────────── App ─────────────────────────
app = FastAPI(title="Cosverse.ai → OpenAI Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthMiddleware)

# ── 路由挂载 ──────────────────────────────────────────────
app.include_router(chat.router)
app.include_router(models.router)
app.include_router(admin.router)


@app.get("/")
async def serve_dashboard():
    html_path = Path("dashboard.html")
    if html_path.exists():
        return FileResponse(html_path)
    return HTMLResponse(
        "<h1>Cosverse Proxy</h1>"
        "<p>OpenAI-compatible proxy for cosverse.ai</p>"
        "<ul>"
        "<li>POST /v1/chat/completions</li>"
        "<li>GET /v1/models</li>"
        "<li>GET /admin/tokens/detail</li>"
        "<li>POST /admin/register/start</li>"
        "<li><del>POST /admin/refresh/all</del> (已废弃：改为按用量动态唤醒防风控)</li>"
        "</ul>"
    )


@app.get("/health")
async def health():
    return {"status": "ok", "accounts": len(cm.accounts)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8008, log_level="info")