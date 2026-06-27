"""
Cosverse.ai 注册与对话核心逻辑
"""
import json
import logging
import mimetypes
import random
import re
import string
from email import message_from_string
from typing import AsyncIterator, Optional

from curl_cffi import CurlMime
from curl_cffi.requests import AsyncSession

from app.config import COSVERSE_WEB, COSVERSE_API, COMMON_HEADERS
from app.action_fetcher import fetch_action_config
from app.models import AccountState
from app.email_service import EmailService

log = logging.getLogger("cosverse_proxy.client")
IMPERSONATE = "chrome110"

R2_UPLOAD_URL = f"{COSVERSE_WEB}/api/r2-upload"


# ── 工具函数 ──────────────────────────────────────────────

def _random_name(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

def _random_conv_id(length: int = 16) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

def _merge_cookies(existing: dict, incoming: dict) -> dict:
    return {**existing, **incoming}

def _cookies_dict_to_str(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())

def _nextjs_headers(action: str, deployment_id: str, router_state: str) -> dict:
    return {
        **COMMON_HEADERS,
        "accept":                 "text/x-component",
        "content-type":           "text/plain;charset=UTF-8",
        "next-action":            action,
        "next-router-state-tree": router_state,
        "priority":               "u=1, i",
        "sec-fetch-dest":         "empty",
        "sec-fetch-mode":         "cors",
        "sec-fetch-site":         "same-origin",
        "x-deployment-id":        deployment_id,
        "referer":                f"{COSVERSE_WEB}/auth/signin",
    }

def _parse_action_response(text: str) -> Optional[dict]:
    try:
        for line in text.strip().split("\n"):
            if line.startswith("1:"):
                return json.loads(line[2:])
    except Exception:
        pass
    return None

def _extract_otp_from_email(raw_email: str) -> Optional[str]:
    if not raw_email:
        return None
    msg = message_from_string(raw_email)
    content = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    content = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            content = payload.decode("utf-8", errors="ignore")
    content = re.sub(r"<[^>]+>", " ", content)
    content = re.sub(r"\s+", " ", content).strip()
    for pattern in [r"(?:code|验证码|OTP).*?(\d{6})", r"(?<!\d)(\d{6})(?!\d)"]:
        m = re.search(pattern, content, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


# ── R2 文件上传 ───────────────────────────────────────────

import base64


async def upload_file_to_cosverse(
        data: bytes,
        filename: str,
        acc: AccountState,
        chat_id: str = "",
        content_type: str = "",
) -> Optional[dict]:
    """
    将文件上传到 Cosverse R2 存储，返回 {"key": ..., "publicUrl": ..., "dataUri": ...} 或 None。
    chat_id 必须与后续 stream_cosverse_response 使用的 conv_id 一致。
    """
    if not content_type:
        content_type, _ = mimetypes.guess_type(filename)
        content_type = content_type or "application/octet-stream"

    # 如果外部未提供 chat_id，则自动补充一个，防止服务器因缺失 chatId 校验而返回 400
    if not chat_id:
        chat_id = _random_conv_id()

    # 使用 curl_cffi 的 CurlMime 对象构建表单
    mp = CurlMime()
    try:
        # 1. 添加文件部分 (data 需为 bytes)
        mp.addpart(
            name="file",
            filename=filename,
            data=data,
            content_type=content_type,
        )

        # 2. 添加 chatId 部分 (显式转换为 bytes 形式，并指定类型为 text/plain)
        mp.addpart(
            name="chatId",
            data=chat_id.encode("utf-8") if isinstance(chat_id, str) else chat_id,
            content_type="text/plain",
        )

        headers = {
            **COMMON_HEADERS,
            "accept": "*/*",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "referer": f"{COSVERSE_WEB}/",
        }

        # 关键避坑点：清除可能冲突的 content-type，保证 curl 自动生成的 boundary 正常生效
        headers.pop("content-type", None)
        headers.pop("Content-Type", None)

        async with AsyncSession(impersonate=IMPERSONATE, timeout=60) as session:
            resp = await session.post(
                R2_UPLOAD_URL,
                headers=headers,
                multipart=mp,
                cookies=acc.cookies_dict(),
                timeout=60,
            )

            if resp.status_code in (200, 201):
                result = resp.json()
                b64_str = base64.b64encode(data).decode('utf-8')
                result["dataUri"] = f"data:{content_type};base64,{b64_str}"

                log.info(f"[{acc.email}] 文件上传成功: {result.get('publicUrl', '')}")
                return result  # {"key": ..., "publicUrl": ..., "dataUri": ...}
            else:
                log.warning(
                    f"[{acc.email}] 文件上传失败: HTTP {resp.status_code} - {resp.text[:200]}"
                )
                return None
    except Exception as e:
        log.error(f"[{acc.email}] 文件上传异常: {e}")
        return None
    finally:
        # 手动释放内存，避免频繁上传带来的开销
        mp.close()


async def upload_url_to_cosverse(
    url: str,
    acc: AccountState,
    chat_id: str = "",
) -> Optional[dict]:
    """
    从 URL 下载图片/文件后上传到 Cosverse R2。
    支持 data URI（base64）和 http/https URL。
    chat_id 必须与后续 stream_cosverse_response 使用的 conv_id 一致。
    """
    import re as _re

    try:
        data_uri_match = _re.match(
            r"data:(?P<mime>[^;,]+)(?:;base64)?,(?P<data>.+)", url, _re.DOTALL
        )
        if data_uri_match:
            mime  = data_uri_match.group("mime").strip()
            b64   = data_uri_match.group("data").strip()
            raw   = base64.b64decode(b64)
            ext   = mimetypes.guess_extension(mime) or ""
            fname = f"upload{ext or '.bin'}"
            return await upload_file_to_cosverse(raw, fname, acc, chat_id, mime)

        async with AsyncSession(impersonate=IMPERSONATE, timeout=30) as session:
            resp = await session.get(url, timeout=30)
            if resp.status_code != 200:
                log.warning(f"下载文件失败: {url} -> HTTP {resp.status_code}")
                return None
            raw          = resp.content
            content_type = resp.headers.get("content-type", "").split(";")[0].strip()
            ext          = mimetypes.guess_extension(content_type) or ""
            path_part    = url.split("?")[0].rstrip("/").split("/")[-1]
            fname        = path_part if "." in path_part else f"download{ext or '.bin'}"
            return await upload_file_to_cosverse(raw, fname, acc, chat_id, content_type)

    except Exception as e:
        log.error(f"upload_url_to_cosverse 失败: {url[:80]} → {e}")
        return None


# ── 注册客户端 ────────────────────────────────────────────

class CosverseRegister:
    """
    注册流程（完全对齐浏览器抓包）：
    Step 1  POST /auth/signin  email_action   body=["email"]
    Step 2  POST /auth/signin  otp_action     body=["email", OTP]
    Step 3  POST /auth/signin  name_action    body=["email", "username"]
    Step 4  GET  /api/auth/csrf
    Step 5  POST /api/auth/callback/credentials
    Step 6  GET  /api/auth/session  → user.id
    """

    def __init__(self):
        self.email_service  = EmailService()
        self.email:    str  = ""
        self.name:     str  = _random_name()
        self.otp:      str  = ""
        self._cookies: dict = {}
        self.user_id:  str  = ""
        self._jwt:     str  = ""
        self._cfg           = None

    def _absorb(self, resp_cookies) -> None:
        self._cookies = _merge_cookies(self._cookies, dict(resp_cookies))

    async def _post_action(self, session: AsyncSession, action: str, body: str) -> tuple[int, str]:
        resp = await session.post(
            f"{COSVERSE_WEB}/auth/signin",
            headers=_nextjs_headers(action, self._cfg.deployment_id, self._cfg.router_state),
            data=body,
            cookies=self._cookies or None,
            timeout=20,
        )
        self._absorb(resp.cookies)
        return resp.status_code, resp.text

    async def step1_send_email(self, session: AsyncSession) -> bool:
        status, text = await self._post_action(
            session, self._cfg.email_action, json.dumps([self.email])
        )
        log.info(f"[{self.email}] Step1 status={status} resp={text[:200]}")
        data = _parse_action_response(text)
        return bool(data and data.get("success"))

    async def step2_verify_otp(self, session: AsyncSession) -> bool:
        status, text = await self._post_action(
            session, self._cfg.otp_action, json.dumps([self.email, self.otp])
        )
        log.info(f"[{self.email}] Step2 OTP={self.otp} status={status} resp={text[:300]}")
        data = _parse_action_response(text)
        return bool(data and data.get("success"))

    async def step3_set_name(self, session: AsyncSession) -> bool:
        status, text = await self._post_action(
            session, self._cfg.name_action, json.dumps([self.email, self.name])
        )
        log.info(f"[{self.email}] Step3 name={self.name} status={status} resp={text[:200]}")
        data = _parse_action_response(text)
        return bool(data and data.get("success"))

    async def step4_nextauth_login(self, session: AsyncSession) -> bool:
        from urllib.parse import quote

        _h_common = {
            **COMMON_HEADERS,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "referer": f"{COSVERSE_WEB}/auth/signin",
        }

        csrf_resp = await session.get(
            f"{COSVERSE_API}/auth/csrf",
            headers={**_h_common, "accept": "*/*", "content-type": "application/json"},
            cookies=self._cookies or None,
            timeout=15,
        )
        self._absorb(csrf_resp.cookies)
        csrf_token = csrf_resp.json().get("csrfToken", "")
        log.info(f"[{self.email}] Step4a csrfToken={'✓' if csrf_token else '✗'}")

        cb_body = (
            f"email={quote(self.email)}&otp={self.otp}"
            f"&csrfToken={csrf_token}"
            f"&callbackUrl={quote(COSVERSE_WEB + '/auth/signin')}"
        )
        cb_resp = await session.post(
            f"{COSVERSE_API}/auth/callback/credentials",
            headers={**_h_common, "accept": "*/*",
                     "content-type": "application/x-www-form-urlencoded",
                     "x-auth-return-redirect": "1"},
            data=cb_body,
            cookies=self._cookies or None,
            timeout=15,
        )
        self._absorb(cb_resp.cookies)
        log.info(f"[{self.email}] Step4b callback status={cb_resp.status_code}")

        sess_resp = await session.get(
            f"{COSVERSE_API}/auth/session",
            headers={**_h_common, "accept": "*/*", "content-type": "application/json"},
            cookies=self._cookies or None,
            timeout=15,
        )
        self._absorb(sess_resp.cookies)
        self.user_id = sess_resp.json().get("user", {}).get("id", "")
        log.info(f"[{self.email}] Step4c user_id={self.user_id}")
        return bool(self.user_id)

    async def _wait_for_otp(self, timeout: int = 120) -> str:
        import asyncio
        for i in range(timeout // 6):
            raw = self.email_service.fetch_first_email(self._jwt)
            if raw:
                otp = _extract_otp_from_email(raw)
                if otp:
                    log.info(f"[{self.email}] 第 {i+1} 次检查到 OTP: {otp}")
                    return otp
            log.info(f"[{self.email}] 第 {i+1} 次检查邮件...")
            await asyncio.sleep(6)
        return ""

    async def run(self) -> Optional[dict]:
        try:
            self._cfg = await fetch_action_config()

            self._jwt, self.email = self.email_service.create_email()
            if not self.email:
                raise RuntimeError("邮箱创建失败")

            async with AsyncSession(impersonate=IMPERSONATE, timeout=60) as session:
                if not await self.step1_send_email(session):
                    raise RuntimeError("Step1 发送邮箱失败")
                self.otp = await self._wait_for_otp()
                if not self.otp:
                    raise RuntimeError("获取 OTP 超时")
                if not await self.step2_verify_otp(session):
                    raise RuntimeError("Step2 OTP 验证失败")
                if not await self.step3_set_name(session):
                    raise RuntimeError("Step3 设置用户名失败")
                if not await self.step4_nextauth_login(session):
                    raise RuntimeError("Step4 next-auth 登录失败")

            account = {
                "email":       self.email,
                "name":        self.name,
                "cookie_str":  _cookies_dict_to_str(self._cookies),
                "user_id":     self.user_id,
                "healthy":     True,
                "disabled":    False,
                "usage_count": 0,
            }
            log.info(f"[{self.email}] ✅ 注册成功 user_id={self.user_id}")
            return account

        except Exception as e:
            log.error(f"CosverseRegister.run() 失败: {e}")
            import traceback; traceback.print_exc()
            return None


# ── 对话客户端 ────────────────────────────────────────────

def messages_to_text(messages: list[dict]) -> str:
    parts = []
    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        parts.append(f"[{role.capitalize()}]: {content}")
    return "\n".join(parts)


def _build_attachment_list(upload_results: list[dict]) -> list[dict]:
    attachments = []
    for r in upload_results:
        if not r:
            continue
        key        = r.get("key", "")
        public_url = r.get("publicUrl", "")
        data_uri   = r.get("dataUri", public_url)
        filename   = key.split("/")[-1] if key else "file"
        mime, _    = mimetypes.guess_type(filename)
        mime       = mime or "application/octet-stream"
        is_image   = mime.startswith("image/")
        attachments.append({
            "key":         key,
            "publicUrl":   public_url,
            "dataUri":     data_uri,
            "name":        filename,
            "contentType": mime,
            "type":        "image" if is_image else "file",
        })
    return attachments


def _chat_body(
    conv_id: str,
    text: str,
    model: str,
    provider: str,
    user_name: str,
    attachments: Optional[list[dict]] = None,
    chat_title: str = "New Chat",
) -> dict:
    user_msg = {
        "role":    "user",
        "content": text,
        "parts":   [{"type": "text", "text": text}],
    }

    if attachments:
        user_msg["experimental_attachments"] = [
            {
                # 抓包确认：浏览器传完整 base64 data URI
                "url":         att.get("dataUri", att.get("publicUrl", "")),
                "filename":    att.get("name", "file"),
                "contentType": att.get("contentType", "image/png"),
            }
            for att in attachments
        ]

    return {
        "id":               conv_id,
        "messages":         [user_msg],
        "model":            model,
        "provider":         provider,
        "conversationId":   conv_id,
        "selectedTools":    [],
        "guestMessageCount": 1,
        "userSettings":     {"userName": user_name, "occupation": "", "traits": "", "preferences": ""},
        "chatTitle":        chat_title,
        "attachments":      [],   # 抓包确认：有图时此字段为空数组
        "pdfAttachments":   [],
    }


async def _fetch_chat_title(
    session: AsyncSession,
    text: str,
    acc: AccountState,
    conv_id: str,
    cfg,
) -> str:
    """
    发 chat 前先调 Next.js Server Action 获取 chatTitle（对齐浏览器行为）。
    失败时静默回退到 "New Chat"，不影响主流程。
    """
    if not cfg or not getattr(cfg, "title_action", None):
        return "New Chat"
    try:
        router_state = cfg.router_state_chat(conv_id)
        resp = await session.post(
            COSVERSE_WEB,
            headers=_nextjs_headers(cfg.title_action, cfg.deployment_id, router_state),
            data=json.dumps([text]),
            cookies=acc.cookies_dict(),
            timeout=15,
        )
        for line in resp.text.strip().split("\n"):
            if line.startswith("1:"):
                title = json.loads(line[2:])
                if isinstance(title, str) and title:
                    log.info(f"[{acc.email}] chatTitle={title}")
                    return title
    except Exception as e:
        log.warning(f"[{acc.email}] 获取 chatTitle 失败（忽略）: {e}")
    return "New Chat"


async def stream_cosverse_response(
    text: str,
    acc: AccountState,
    model: str = "gemini-3-flash-preview",
    provider: str = "google",
    attachments: Optional[list[dict]] = None,
    conv_id: Optional[str] = None,
) -> AsyncIterator[str]:
    """
    向 Cosverse /api/chat 发送请求并流式返回文本块。

    :param attachments: _build_attachment_list() 返回的列表（r2 上传结果）
    :param conv_id:     对话 ID。有附件时必须与上传时传入的 chat_id 相同。
                        不传则自动生成（无附件场景）。
    """
    if conv_id is None:
        conv_id = _random_conv_id()

    cfg = await fetch_action_config()

    async with AsyncSession(impersonate=IMPERSONATE, timeout=120) as session:
        chat_title = await _fetch_chat_title(session, text, acc, conv_id, cfg)
        body = _chat_body(conv_id, text, model, provider, acc.name or acc.email, attachments, chat_title)

        resp = await session.post(
            f"{COSVERSE_API}/chat",
            headers={
                **COMMON_HEADERS,
                "accept":         "*/*",
                "content-type":   "application/json",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "referer":        f"{COSVERSE_WEB}/",
            },
            json=body,
            cookies=acc.cookies_dict(),
            stream=True,
            timeout=120,
        )
        if resp.status_code == 401:
            raise PermissionError(f"cookie_expired:{acc.email}")
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Cosverse /api/chat 返回 {resp.status_code}")

        async for raw_line in resp.aiter_lines():
            line = (
                raw_line.decode("utf-8", errors="ignore")
                if isinstance(raw_line, bytes)
                else raw_line
            )
            if not line:
                continue
            if line.startswith("0:"):
                chunk = line[2:]
                try:
                    chunk = json.loads(chunk)
                except Exception:
                    pass
                if chunk:
                    yield chunk
            elif line.startswith("e:") or line.startswith("d:"):
                return