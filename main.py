"""
Cosverse.ai → OpenAI-compatible API Proxy
入口文件
"""
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


# ─────────────────────────── 定时保活 ────────────────────
async def keep_alive_task():
    """后台定时保活程序：防止 cookie 过期"""
    while True:
        try:
            # 每隔 2 小时执行一次保活
            await asyncio.sleep(7200)
            logging.getLogger("cosverse_proxy.keepalive").info("🔄 开始执行定时保活任务...")

            for acc in cm.accounts:
                # 仅对健康且额度未满的账号进行保活
                if not acc.disabled and acc.usage_count < 100:
                    await cm.check_session(acc)
                    await asyncio.sleep(3)  # 防止并发过高，错峰请求

        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.getLogger("cosverse_proxy.keepalive").error(f"保活任务异常: {e}")


# ─────────────────────────── 生命周期 ────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    cm.load()
    from app.action_fetcher import fetch_action_config
    await fetch_action_config()
    logging.getLogger("cosverse_proxy").info(
        f"🚀 代理服务启动，已加载 {len(cm.accounts)} 个账号"
    )

    # 启动后台保活任务
    bg_task = asyncio.create_task(keep_alive_task())

    yield

    bg_task.cancel()


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
        "</ul>"
    )


@app.get("/health")
async def health():
    return {"status": "ok", "accounts": len(cm.accounts)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8008, log_level="info")