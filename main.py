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
# 保活间隔：每 2 小时执行一次 session 检查 + 失效账号重新登录
KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", "7200"))

_ka_log = logging.getLogger("cosverse_proxy.keepalive")


async def keep_alive_task():
    """
    后台定时保活任务。
    每次触发时：
      1. 检查所有未禁用账号的 session 是否仍然有效。
      2. 对失效账号调用 CosverseRefresher 重新走邮箱登录流程，拿新 cookie。
      3. 检查备用账号数量是否低于阈值，不足时触发注册。
    """
    # 启动后先等待一个完整周期，避免刚加载就立刻跑保活
    _ka_log.info(f"⏳ 保活任务已启动，首次执行将在 {KEEPALIVE_INTERVAL // 60} 分钟后")
    await asyncio.sleep(KEEPALIVE_INTERVAL)

    while True:
        try:
            _ka_log.info("🔄 开始执行定时保活...")
            await cm.keepalive_tick()
            _ka_log.info(f"✅ 保活完成，下次执行在 {KEEPALIVE_INTERVAL // 60} 分钟后")
        except asyncio.CancelledError:
            _ka_log.info("保活任务已取消")
            break
        except Exception as e:
            _ka_log.error(f"保活任务异常: {e}", exc_info=True)
        finally:
            # 无论成功或失败，都等待下一个周期
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
            except asyncio.CancelledError:
                pass


# ─────────────────────────── 生命周期 ────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    cm.load()
    from app.action_fetcher import fetch_action_config
    await fetch_action_config()
    logging.getLogger("cosverse_proxy").info(
        f"🚀 代理服务启动，已加载 {len(cm.accounts)} 个账号"
    )

    bg_task = asyncio.create_task(keep_alive_task())

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
        "<li>POST /admin/refresh/all  — 手动触发全量保活</li>"
        "</ul>"
    )


@app.get("/health")
async def health():
    return {"status": "ok", "accounts": len(cm.accounts)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8008, log_level="info")