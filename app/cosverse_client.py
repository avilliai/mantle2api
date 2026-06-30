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


class EmptyResponseError(RuntimeError):
    """流式响应内容为空（content_tokens=0），视作 cookie 过期或账号异常。"""
    pass

class ServerError(RuntimeError):
    """Cosverse 服务端应用层错误（如 invalid_grant），无需重试，直接透传给调用方。"""
    pass

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

    if not chat_id:
        chat_id = _random_conv_id()

    mp = CurlMime()
    try:
        mp.addpart(
            name="file",
            filename=filename,
            data=data,
            content_type=content_type,
        )
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
                return result
            else:
                log.warning(f"[{acc.email}] 文件上传失败: HTTP {resp.status_code} - {resp.text[:200]}")
                return None
    except Exception as e:
        log.error(f"[{acc.email}] 文件上传异常: {e}")
        return None
    finally:
        mp.close()


async def upload_url_to_cosverse(
    url: str,
    acc: AccountState,
    chat_id: str = "",
) -> Optional[dict]:
    """
    从 URL 下载图片/文件后上传到 Cosverse R2。
    支持 data URI（base64）和 http/https URL。
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


# ── 登录公共 Mixin ────────────────────────────────────────

class _LoginMixin:
    """
    封装 email_action / otp_action / nextauth 三步登录的公共实现，
    供 CosverseRegister 和 CosverseRefresher 共同继承，避免重复代码。
    """

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

    async def _step_send_email(self, session: AsyncSession, email: str) -> bool:
        status, text = await self._post_action(
            session, self._cfg.email_action, json.dumps([email])
        )
        log.info(f"[{email}] send_email status={status} resp={text[:200]}")
        data = _parse_action_response(text)
        return bool(data and data.get("success"))

    async def _step_verify_otp(self, session: AsyncSession, email: str, otp: str) -> bool:
        status, text = await self._post_action(
            session, self._cfg.otp_action, json.dumps([email, otp])
        )
        log.info(f"[{email}] verify_otp OTP={otp} status={status} resp={text[:300]}")
        data = _parse_action_response(text)
        return bool(data and data.get("success"))

    async def _step_nextauth_login(self, session: AsyncSession, email: str, otp: str) -> str:
        """
        走 csrf → callback/credentials → session 流程。
        成功返回 user_id 字符串，失败返回空字符串。
        """
        from urllib.parse import quote

        _h_common = {
            **COMMON_HEADERS,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "referer": f"{COSVERSE_WEB}/auth/signin",
        }

        # 4a. 获取 CSRF token
        csrf_resp = await session.get(
            f"{COSVERSE_API}/auth/csrf",
            headers={**_h_common, "accept": "*/*", "content-type": "application/json"},
            cookies=self._cookies or None,
            timeout=15,
        )
        self._absorb(csrf_resp.cookies)
        csrf_token = csrf_resp.json().get("csrfToken", "")
        log.info(f"[{email}] csrfToken={'✓' if csrf_token else '✗'}")

        # 4b. callback/credentials
        cb_body = (
            f"email={quote(email)}&otp={otp}"
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
        log.info(f"[{email}] callback status={cb_resp.status_code}")

        # 4c. 验证 session
        sess_resp = await session.get(
            f"{COSVERSE_API}/auth/session",
            headers={**_h_common, "accept": "*/*", "content-type": "application/json"},
            cookies=self._cookies or None,
            timeout=15,
        )
        self._absorb(sess_resp.cookies)
        user_id = sess_resp.json().get("user", {}).get("id", "")
        log.info(f"[{email}] session user_id={user_id}")
        return user_id

    async def _wait_for_otp(self, email_service: EmailService, jwt: str,
                             email: str, timeout: int = 120) -> str:
        import asyncio
        for i in range(timeout // 6):
            raw = email_service.fetch_first_email(jwt)
            if raw:
                otp = _extract_otp_from_email(raw)
                if otp:
                    log.info(f"[{email}] 第 {i+1} 次检查到 OTP: {otp}")
                    return otp
            log.info(f"[{email}] 第 {i+1} 次检查邮件...")
            await asyncio.sleep(6)
        return ""


# ── 注册客户端 ────────────────────────────────────────────

class CosverseRegister(_LoginMixin):
    """
    注册流程：
    Step 1  发送邮箱（触发 OTP）
    Step 2  验证 OTP
    Step 3  设置用户名（仅注册需要）
    Step 4  nextauth 登录拿 session cookie
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

    async def _step_set_name(self, session: AsyncSession) -> bool:
        status, text = await self._post_action(
            session, self._cfg.name_action, json.dumps([self.email, self.name])
        )
        log.info(f"[{self.email}] Step3 name={self.name} status={status} resp={text[:200]}")
        data = _parse_action_response(text)
        return bool(data and data.get("success"))

    async def run(self) -> Optional[dict]:
        try:
            self._cfg = await fetch_action_config()

            self._jwt, self.email = self.email_service.create_email()
            if not self.email:
                raise RuntimeError("邮箱创建失败")

            async with AsyncSession(impersonate=IMPERSONATE, timeout=60) as session:
                if not await self._step_send_email(session, self.email):
                    raise RuntimeError("Step1 发送邮箱失败")

                self.otp = await self._wait_for_otp(
                    self.email_service, self._jwt, self.email
                )
                if not self.otp:
                    raise RuntimeError("获取 OTP 超时")

                if not await self._step_verify_otp(session, self.email, self.otp):
                    raise RuntimeError("Step2 OTP 验证失败")

                if not await self._step_set_name(session):
                    raise RuntimeError("Step3 设置用户名失败")

                self.user_id = await self._step_nextauth_login(
                    session, self.email, self.otp
                )
                if not self.user_id:
                    raise RuntimeError("Step4 next-auth 登录失败")

            account = {
                "email":       self.email,
                "name":        self.name,
                "cookie_str":  _cookies_dict_to_str(self._cookies),
                "user_id":     self.user_id,
                "email_jwt":   self._jwt,       # 保存邮箱 JWT，供保活使用
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


# ── 保活客户端 ────────────────────────────────────────────

class CosverseRefresher(_LoginMixin):
    """
    保活流程（已有邮箱重新登录，跳过设置用户名）：
    Step 1  发送邮箱（触发 OTP）
    Step 2  等待并获取 OTP
    Step 3  验证 OTP
    Step 4  nextauth 登录拿新 session cookie
    成功后直接将新 cookie 写回 AccountState 并标记为健康。
    """

    def __init__(self, acc: AccountState):
        self.acc            = acc
        self.email          = acc.email
        self.otp:     str   = ""
        self._cookies: dict = {}
        self._cfg           = None
        # 优先从 AccountState 取已保存的邮箱 JWT
        self._email_jwt: str = getattr(acc, "email_jwt", "")

    async def _recover_email_jwt(self) -> str:
        """
        当 AccountState 里没有保存 email_jwt，或已知 JWT 过期时，
        通过管理员接口按邮箱名查 address_id，再调 show_password 取回 JWT。
        网络超时时不抛出异常，直接返回空字符串。
        """
        try:
            from app.email_service import EmailService as ES
            import asyncio
            es = ES()
            name = self.email.split("@")[0]
            # 整体限时 20s，避免单次超时拖垮保活流程
            jwt, _ = await asyncio.wait_for(
                asyncio.to_thread(es.recover_email_jwt, name),
                timeout=20,
            )
            if jwt:
                log.info(f"[{self.email}] 成功取回 email_jwt")
                self.acc.email_jwt = jwt
                self._email_jwt = jwt
            return jwt or ""
        except Exception as e:
            log.warning(f"[{self.email}] 取回 email_jwt 失败: {e}")
            return ""

    async def _ensure_valid_email_jwt(self) -> str:
        """
        验证当前 email_jwt 是否可用（能正常拉到邮件列表），
        不可用（过期/无效）时自动重新获取一次。
        返回可用的 JWT 或空字符串。
        """
        if not self._email_jwt:
            log.warning(f"[{self.email}] 无 email_jwt，尝试从邮件服务恢复...")
            return await self._recover_email_jwt()

        # 快速探测：尝试拉取邮件列表，401/403 → JWT 已过期
        try:
            from app.email_service import EmailService as ES
            import asyncio
            es = ES()

            def _probe(jwt: str):
                import requests
                res = requests.get(
                    f"https://{es.worker_domain}/api/mails",
                    params={"limit": 1, "offset": 0},
                    headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
                    timeout=8,
                    verify=False,
                )
                return res.status_code

            status = await asyncio.wait_for(
                asyncio.to_thread(_probe, self._email_jwt),
                timeout=12,
            )
            if status in (200,):
                return self._email_jwt  # JWT 仍然有效

            log.warning(f"[{self.email}] email_jwt 探测返回 {status}，视为过期，重新获取...")
        except Exception as e:
            log.warning(f"[{self.email}] email_jwt 探测异常: {e}，尝试重新获取...")

        # JWT 过期或探测失败 → 重新获取
        return await self._recover_email_jwt()

    async def run(self) -> bool:
        """
        执行保活。成功后更新 acc.cookie_str / acc.healthy / acc.disabled。
        :return: True=成功，False=失败
        """
        try:
            self._cfg = await fetch_action_config()

            # 确保有可用的邮箱 JWT（自动处理缺失和过期两种情况）
            self._email_jwt = await self._ensure_valid_email_jwt()
            if not self._email_jwt:
                raise RuntimeError(
                    f"无法获取 email_jwt（账号 {self.email} 注册时未保存，"
                    f"且通过管理员接口恢复也失败）。"
                    f"请检查 WORKER_DOMAIN / ADMIN_PASSWORD 配置，或手动补填 tokens.json 中的 email_jwt 字段。"
                )

            email_service = EmailService()

            async with AsyncSession(impersonate=IMPERSONATE, timeout=60) as session:
                # Step 1：触发 OTP 邮件
                if not await self._step_send_email(session, self.email):
                    raise RuntimeError("Step1 发送邮箱失败")

                # Step 2：等待并读取 OTP
                self.otp = await self._wait_for_otp(
                    email_service, self._email_jwt, self.email
                )
                if not self.otp:
                    raise RuntimeError("获取 OTP 超时")

                # Step 3：验证 OTP
                if not await self._step_verify_otp(session, self.email, self.otp):
                    raise RuntimeError("Step2 OTP 验证失败")

                # Step 4：nextauth 登录（注意：保活不走 set_name）
                user_id = await self._step_nextauth_login(
                    session, self.email, self.otp
                )
                if not user_id:
                    raise RuntimeError("Step3 next-auth 登录失败，session 未返回 user_id")

            # ✅ 写回 AccountState
            self.acc.cookie_str = _cookies_dict_to_str(self._cookies)
            self.acc.healthy    = True
            self.acc.disabled   = False
            log.info(f"[{self.email}] ✅ 保活成功，cookie 已更新")
            return True

        except Exception as e:
            log.error(f"[{self.email}] ❌ 保活失败: {e}")
            import traceback; traceback.print_exc()
            return False


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
        "attachments":      [],
        "pdfAttachments":   [],
    }


async def _fetch_chat_title(
    session: AsyncSession,
    text: str,
    acc: AccountState,
    conv_id: str,
    cfg,
) -> str:
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
    若响应 HTTP 200 但所有 chunk 内容为空（completion_tokens=0），
    抛出 EmptyResponseError，由调用方触发 refresh + 重试逻辑。
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

        yielded_any = False
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
                    yielded_any = True
                    yield chunk
            elif line.startswith("3:"):
                # 服务端应用层错误（如 invalid_grant），与 cookie 无关，直接抛出不重试
                try:
                    err_msg = json.loads(line[2:])
                except Exception:
                    err_msg = line[2:]
                log.error(f"[{acc.email}] 服务端错误: {err_msg}")
                raise ServerError(str(err_msg))
            elif line.startswith("e:") or line.startswith("d:"):
                break

        # HTTP 200 但无任何有效文本块 → 视为 cookie 过期 / 账号异常
        if not yielded_any:
            raise EmptyResponseError(f"empty_response:{acc.email}")

# ── 带重试的对话入口（供 chat router 调用）────────────────

async def stream_with_retry(
    text: str,
    model: str,
    provider: str,
    attachments: Optional[list[dict]] = None,
    conv_id: Optional[str] = None,
) -> AsyncIterator[str]:
    """
    带自动重试的流式对话入口。
    失败策略：
      1. 当前账号空响应或 cookie 过期 → 对该账号执行 refresh，重试一次；
      2. refresh 后重试仍失败 → 切换到号池中的下一个账号，再重试一次；
      3. 连换两个账号都失败 → 注册新账号加入号池，用新账号再试；
      4. 新账号仍失败 → 抛出最终异常。
    注意：ServerError（服务端应用层错误，如 invalid_grant）直接透传，不触发任何重试。
    """
    from app.cookie_manager import cm

    _TRANSIENT_ERRORS = (PermissionError, EmptyResponseError)

    async def _try_stream(acc: AccountState) -> AsyncIterator[str]:
        """直接迭代并 yield，异常向上抛出。"""
        async for chunk in stream_cosverse_response(
            text, acc, model, provider, attachments, conv_id
        ):
            yield chunk

    async def _refresh_and_retry(acc: AccountState) -> AsyncIterator[str]:
        """对 acc 执行 refresh，成功则重试流式请求，失败则抛出。"""
        log.warning(f"[{acc.email}] 触发 refresh 后重试...")
        ok = await cm.refresh_account(acc)
        if not ok:
            raise RuntimeError(f"[{acc.email}] refresh 失败，无法重试")
        async for chunk in _try_stream(acc):
            yield chunk

    # ── 第一次尝试 ──────────────────────────────────────
    acc = await cm.get_next()
    try:
        async for chunk in _try_stream(acc):
            yield chunk
        return
    except ServerError:
        # 服务端错误与 cookie 无关，直接向上抛出，不重试
        raise
    except _TRANSIENT_ERRORS as e:
        log.warning(f"首次请求失败（{type(e).__name__}）: {e}")

    # ── 第二次：对同一账号 refresh 后重试 ───────────────
    try:
        async for chunk in _refresh_and_retry(acc):
            yield chunk
        return
    except ServerError:
        raise
    except Exception as e:
        log.warning(f"refresh 后重试仍失败: {e}")

    # ── 第三次：换备用账号 ──────────────────────────────
    try:
        acc2 = await cm.mark_failed_and_get_replacement(acc, reason="empty_or_expired", attempt=2)
        async for chunk in _try_stream(acc2):
            yield chunk
        return
    except ServerError:
        raise
    except _TRANSIENT_ERRORS as e:
        log.warning(f"备用账号（{acc2.email}）也失败: {e}")
    except Exception as e:
        log.warning(f"切换备用账号异常: {e}")

    # ── 第四次：注册新账号 ──────────────────────────────
    log.warning("两个账号均失败，尝试注册新账号...")
    try:
        acc3 = await cm.mark_failed_and_get_replacement(acc, reason="all_failed", attempt=3)
        async for chunk in _try_stream(acc3):
            yield chunk
        return
    except Exception as e:
        log.error(f"注册新账号后仍失败: {e}")
        raise RuntimeError("所有重试策略均已耗尽，对话请求失败") from e