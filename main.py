"""
Mantle.chat -> OpenAI-compatible API Proxy
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

API_KEY = os.getenv("PROXY_API_KEY", "endlesswork")
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
        if API_KEY and token != API_KEY:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid API key", "type": "auth_error"}},
            )
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cm.load()
    logging.getLogger("mantle_proxy").info(
        f"代理服务启动，已加载 {len(cm.accounts)} 个 Mantle 账号"
    )

    # 启动时异步同步所有已启用账号的真实月度配额与保活状态
    for acc in cm.accounts:
        if not acc.disabled:
            asyncio.create_task(cm.check_session(acc))

    yield


app = FastAPI(title="Mantle.chat -> OpenAI Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthMiddleware)

app.include_router(chat.router)
app.include_router(models.router)
app.include_router(admin.router)


@app.get("/")
async def serve_dashboard():
    html_path = Path("dashboard.html")
    if html_path.exists():
        return FileResponse(html_path)
    return HTMLResponse(
        "<h1>Mantle Chat Proxy</h1>"
        "<p>OpenAI-compatible proxy for mantle.chat</p>"
        "<ul>"
        "<li>POST /v1/chat/completions</li>"
        "<li>GET /v1/models</li>"
        "<li>GET /admin/tokens/detail</li>"
        "<li>POST /admin/register/start</li>"
        "</ul>"
    )


@app.get("/health")
async def health():
    eligible = sum(1 for a in cm.accounts if cm._account_is_eligible(a))
    return {"status": "ok", "total_accounts": len(cm.accounts), "available_accounts": eligible}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8008"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")