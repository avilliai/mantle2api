"""
Mantle.chat ???????????? (??? curl_cffi?? Playwright ??)
"""
import asyncio
import json
import logging
import os
import re
import uuid
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse, parse_qs

from curl_cffi.requests import AsyncSession
from curl_cffi.curl import CurlMime

from app.config import MANTLE_API, MANTLE_WEB, HTTP_PROXY
from app.email_service import EmailService
from app.models import AccountState

log = logging.getLogger("mantle_proxy.client")
IMPERSONATE = "chrome120"


class EmptyResponseError(RuntimeError):
    pass


class ServerError(RuntimeError):
    pass


def messages_to_text(messages: list[dict]) -> str:
    """? OpenAI ??? messages ??????? prompt ???????"""
    if not messages:
        return ""
    if len(messages) == 1 and messages[0].get("role") == "user":
        content = messages[0].get("content", "")
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "\n".join(parts)

    formatted = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            text = "\n".join(parts)

        if role == "system":
            formatted.append(f"[System Instruction: {text}]")
        elif role == "assistant":
            formatted.append(f"Assistant: {text}")
        elif role == "user":
            formatted.append(f"User: {text}")
        elif role == "tool":
            formatted.append(f"[Tool Response: {text}]")
        else:
            formatted.append(f"{role.capitalize()}: {text}")

    return "\n\n".join(formatted)


class MantleRegister:
    """????? Mantle.chat ???? curl_cffi ??????????"""

    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy or HTTP_PROXY
        self.email_service = EmailService()
        self.email_domain = os.getenv("EMAIL_DOMAIN", "denia.de5.net")
        self.email_service.email_domain = self.email_domain

    async def run(self) -> Optional[dict]:
        jwt, email_addr = self.email_service.create_email()
        log.info(f"?? ???? Mantle.chat ?? (? API/curl_cffi): {email_addr}")

        try:
            async with AsyncSession(proxy=self.proxy or None, impersonate=IMPERSONATE) as s:
                # 1. ?? mantle login ?? workos ???
                log.info("1. ?? Mantle WorkOS ?????...")
                r1 = await s.get(
                    "https://mantle.chat/api/auth/workos/login?provider=authkit&returnUrl=%2Fchat",
                    allow_redirects=True,
                    timeout=30,
                )
                if r1.status_code >= 400:
                    log.error(f"?????????: HTTP {r1.status_code}")
                    return None

                parsed = urlparse(r1.url)
                qs = parse_qs(parsed.query)
                client_id = qs.get("client_id", [""])[0]
                redirect_uri = qs.get("redirect_uri", [""])[0]
                state = qs.get("state", [""])[0]
                session_id = qs.get("authorization_session_id", [""])[0]

                if not session_id or not redirect_uri:
                    log.error(f"????? authorization_session_id ? redirect_uri: {r1.url}")
                    return None

                # 2. ????
                log.info(f"2. ? AuthKit ????: {email_addr}...")
                mime = CurlMime()
                mime.addpart(name="_1_email", data=email_addr.encode("utf-8"))
                mime.addpart(name="_1_redirect_uri", data=redirect_uri.encode("utf-8"))
                mime.addpart(name="_1_authorization_session_id", data=session_id.encode("utf-8"))
                mime.addpart(name="_1_state", data=state.encode("utf-8"))
                mime.addpart(name="0", data=b'["$K1"]')

                headers_submit = {
                    "Next-Action": "409dfdd69397fa9f326db3612977e0aa51bada7cdb",
                    "Accept": "text/x-component",
                    "Referer": r1.url,
                    "Origin": f"{parsed.scheme}://{parsed.netloc}",
                }

                r2 = await s.post(r1.url, multipart=mime, headers=headers_submit, timeout=30)
                redir = r2.headers.get("x-action-redirect", "")
                if not redir:
                    log.error(f"?????????? header: status={r2.status_code}, resp={r2.text[:200]}")
                    return None

                redir_path = redir.split(";")[0]
                redir_qs = parse_qs(urlparse(redir_path).query)
                challenge_id = redir_qs.get("authentication_challenge_id", [""])[0]
                radar_id = redir_qs.get("radar_auth_attempt_id", [""])[0]
                radar_created_at = redir_qs.get("radar_auth_attempt_created_at", [""])[0]
                magic_state = redir_qs.get("state", [""])[0]

                # 3. ???????
                log.info("3. ?????? 6 ????...")
                code = None
                for _ in range(30):
                    await asyncio.sleep(2)
                    try:
                        raw_mail = self.email_service.fetch_first_email(jwt)
                        if raw_mail:
                            m = re.findall(r"\b\d{6}\b", raw_mail)
                            if m:
                                code = m[0]
                                log.info(f"? ???????: {code}")
                                break
                    except Exception as e:
                        log.debug(f"???????: {e}")

                if not code:
                    log.error(f"?? OTP ?????: {email_addr}")
                    return None

                # 4. ?????
                log.info("4. ?? OTP ???? AuthKit...")
                mime_otp = CurlMime()
                mime_otp.addpart(name="_1_code", data=code.encode("utf-8"))
                mime_otp.addpart(name="_1_redirect_uri", data=redirect_uri.encode("utf-8"))
                mime_otp.addpart(name="_1_authorization_session_id", data=session_id.encode("utf-8"))
                mime_otp.addpart(name="_1_state", data=magic_state.encode("utf-8"))
                mime_otp.addpart(name="_1_email", data=email_addr.encode("utf-8"))
                mime_otp.addpart(name="_1_authentication_challenge_id", data=challenge_id.encode("utf-8"))
                mime_otp.addpart(name="_1_radar_auth_attempt_id", data=radar_id.encode("utf-8"))
                mime_otp.addpart(name="_1_radar_auth_attempt_created_at", data=radar_created_at.encode("utf-8"))
                mime_otp.addpart(name="0", data=b'["$K1"]')

                otp_url = f"{parsed.scheme}://{parsed.netloc}{redir_path}"
                headers_otp = {
                    "Next-Action": "407aa320ed3a3d9010c11a0a290f46c737a94ddb35",
                    "Accept": "text/x-component",
                    "Referer": otp_url,
                    "Origin": f"{parsed.scheme}://{parsed.netloc}",
                }

                r3 = await s.post(otp_url, multipart=mime_otp, headers=headers_otp, timeout=30)
                if r3.status_code != 200:
                    log.error(f"?????????: HTTP {r3.status_code}")
                    return None

                # 5. ?? AuthKit ???????? (RSC Payload)
                log.info("5. ?? AuthKit ????????...")
                rsc_target = f"{parsed.scheme}://{parsed.netloc}/success?client_redirect_key={session_id}&_rsc"
                r_rsc = await s.get(rsc_target, headers={"RSC": "1"}, allow_redirects=False, timeout=30)

                match = re.search(r'"clientRedirect":"([^"]+)"', r_rsc.text)
                if not match:
                    log.error(f"??? AuthKit RSC ?????? clientRedirect")
                    return None

                client_redirect_url = match.group(1).encode("utf-8").decode("unicode_escape")

                # 6. ?? Mantle ??????? Session Cookie
                log.info("6. ?? Mantle ????????...")
                r_cb = await s.get(client_redirect_url, allow_redirects=True, timeout=30)
                if r_cb.status_code >= 400:
                    log.error(f"Mantle ????: HTTP {r_cb.status_code}")
                    return None

                mantle_cookies = {}
                for c in s.cookies.jar:
                    if "mantle.chat" in getattr(c, "domain", ""):
                        mantle_cookies[c.name] = c.value

                # 7. ?? /api/auth/me, /api/workspaces, /api/auth/csrf
                log.info("7. ???? User ID ? Workspace ??...")
                r_me = await s.get(f"{MANTLE_API}/auth/me", timeout=15)
                if r_me.status_code != 200:
                    log.error(f"?? /api/auth/me ??: HTTP {r_me.status_code}")
                    return None
                me_data = r_me.json()
                user_id = me_data.get("id", "")
                auth_uid = me_data.get("authUid", "")

                r_ws = await s.get(f"{MANTLE_API}/workspaces", timeout=15)
                ws_data = r_ws.json()
                workspace_id = ws_data[0]["id"] if isinstance(ws_data, list) and ws_data else None

                r_csrf = await s.get(f"{MANTLE_API}/auth/csrf", timeout=15)
                csrf_token = r_csrf.json().get("csrfToken", "") if r_csrf.status_code == 200 else ""

                if not user_id or not workspace_id:
                    log.error(f"?????????: user_id={user_id}, ws={workspace_id}")
                    return None

                account_data = {
                    "email": email_addr,
                    "user_id": user_id,
                    "auth_uid": auth_uid,
                    "workspace_id": workspace_id,
                    "csrf_token": csrf_token,
                    "cookies": mantle_cookies,
                    "healthy": True,
                    "message_limit": 1500,
                    "message_usage": 0,
                    "usage_count": 0,
                    "quota_exhausted": False,
                }
                log.info(f"?? ??????! Email={email_addr}, Workspace={workspace_id}")
                return account_data

        except Exception as e:
            log.error(f"? curl_cffi ????????: {e}", exc_info=True)
            return None


async def upload_file_to_mantle(
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    account: AccountState,
    chat_id: Optional[str] = None,
) -> Optional[str]:
    """
    ????/??? Mantle ? R2 ???????? attachmentId?
    """
    ws = account.workspace_id
    if not ws:
        log.error(f"[{account.email}] ?? workspace_id???????")
        return None

    if not mime_type:
        mime_type = "application/octet-stream"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": MANTLE_WEB,
        "Referer": f"{MANTLE_WEB}/w/{ws}/chat",
        "x-csrf-token": account.csrf_token,
    }

    req_body = {
        "workspaceId": ws,
        "purpose": "chat",
        "filename": filename,
        "mimeType": mime_type,
        "sizeBytes": len(file_bytes),
        "supportsConditionalPut": True,
    }
    if chat_id:
        req_body["chatId"] = chat_id

    try:
        async with AsyncSession(proxy=HTTP_PROXY or None, impersonate=IMPERSONATE) as session:
            for k, v in account.cookies.items():
                session.cookies.set(k, v, domain="mantle.chat")

            # 1. ?????????
            r = await session.post(
                f"{MANTLE_API}/attachments/upload-url",
                json=req_body,
                headers=headers,
                timeout=30,
            )
            if r.status_code != 200:
                log.error(f"[{account.email}] ?????????? ({r.status_code}): {r.text}")
                return None

            res_data = r.json()
            attachment_id = res_data.get("attachmentId")
            upload_url = res_data.get("uploadUrl")
            if not attachment_id or not upload_url:
                log.error(f"[{account.email}] ?????????: {res_data}")
                return None

            # 2. PUT ????? Cloudflare R2
            put_headers = {
                "Content-Type": mime_type,
                "If-None-Match": "*",
            }
            r_put = await session.put(upload_url, data=file_bytes, headers=put_headers, timeout=60)
            if r_put.status_code not in (200, 201, 204):
                log.error(f"[{account.email}] ??? R2 ?? ({r_put.status_code}): {r_put.text}")
                return None

            # 3. ????
            r_act = await session.post(
                f"{MANTLE_API}/attachments/{attachment_id}/activate",
                json={},
                headers=headers,
                timeout=30,
            )
            if r_act.status_code != 200:
                log.error(f"[{account.email}] ?????? ({r_act.status_code}): {r_act.text}")
                return None

            log.info(f"[{account.email}] ?????????: {attachment_id} ({filename})")
            return attachment_id
    except Exception as e:
        log.error(f"[{account.email}] ??????: {e}", exc_info=True)
        return None


async def upload_url_to_mantle(
    url: str,
    account: AccountState,
    chat_id: Optional[str] = None,
) -> Optional[str]:
    """
    ? data URI ? http/https URL ??/???????? Mantle??? attachmentId?
    """
    import base64
    import mimetypes

    try:
        # ?? data URI (base64)
        data_uri_match = re.match(
            r"data:(?P<mime>[^;,]+)(?:;base64)?,(?P<data>.+)", url, re.DOTALL
        )
        if data_uri_match:
            mime = data_uri_match.group("mime").strip()
            raw_data = base64.b64decode(data_uri_match.group("data").strip())
            ext = mimetypes.guess_extension(mime) or ""
            filename = f"upload{ext or '.bin'}"
            return await upload_file_to_mantle(raw_data, filename, mime, account, chat_id)

        # ?? URL ??
        async with AsyncSession(proxy=HTTP_PROXY or None, impersonate=IMPERSONATE, timeout=30) as session:
            resp = await session.get(url, timeout=30)
            if resp.status_code != 200:
                log.warning(f"??????: {url} -> HTTP {resp.status_code}")
                return None
            raw_data = resp.content
            mime = resp.headers.get("content-type", "").split(";")[0].strip() or "application/octet-stream"
            ext = mimetypes.guess_extension(mime) or ""
            path_part = url.split("?")[0].rstrip("/").split("/")[-1]
            filename = path_part if "." in path_part else f"download{ext or '.bin'}"
            return await upload_file_to_mantle(raw_data, filename, mime, account, chat_id)
    except Exception as e:
        log.error(f"upload_url_to_mantle ??: {url[:80]} - {e}")
        return None


async def stream_mantle_response(
    account: AccountState,
    model: str,
    prompt: str,
    chat_id: Optional[str] = None,
    attachments: Optional[list[str]] = None,
) -> AsyncIterator[dict]:
    """
    ?? Mantle /api/chat/create-stream ??? SSE /api/chat/stream/{streamId}
    yield SSE delta ???? OpenAI ??????
    """
    ws = account.workspace_id
    if not ws:
        raise ValueError(f"?? {account.email} ?? workspace_id")

    chat_id = chat_id or str(uuid.uuid4())

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": MANTLE_WEB,
        "Referer": f"{MANTLE_WEB}/w/{ws}/chat",
        "x-csrf-token": account.csrf_token,
    }

    cookies = dict(account.cookies)

    payload = {
        "prompt": prompt,
        "userId": account.user_id,
        "chatId": chat_id,
        "model": model,
        "streamResponse": True,
        "workspaceId": ws,
        "webSearchEnabled": False,
        "reasoningEnabled": False,
        "reasoningEffort": "none",
        "autoSave": True,
        "isTemporaryChat": False,
    }

    if attachments:
        payload["attachments"] = attachments

    async with AsyncSession(proxy=HTTP_PROXY or None, impersonate=IMPERSONATE) as session:
        for k, v in cookies.items():
            session.cookies.set(k, v, domain="mantle.chat")

        resp = await session.post(
            f"{MANTLE_API}/chat/create-stream",
            json=payload,
            headers=headers,
            timeout=30,
        )

        if resp.status_code != 200:
            err_msg = f"Mantle create-stream error {resp.status_code}: {resp.text}"
            log.error(err_msg)
            raise ServerError(err_msg)

        data = resp.json()
        stream_id = data.get("streamId")
        if not stream_id:
            raise ServerError(f"??? streamId: {resp.text}")

        stream_url = f"{MANTLE_API}/chat/stream/{stream_id}"
        stream_resp = await session.get(stream_url, headers=headers, stream=True, timeout=60)

        if stream_resp.status_code != 200:
            err_msg = f"Mantle stream error {stream_resp.status_code}: {stream_resp.text}"
            log.error(err_msg)
            raise ServerError(err_msg)

        has_content = False
        async for line in stream_resp.aiter_lines():
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")

            if line.startswith("data: "):
                raw = line[6:].strip()
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                    event_type = event.get("type")
                    if event_type == "delta":
                        content = event.get("content", "")
                        if content:
                            has_content = True
                            yield {"delta": content}
                    elif event_type == "final":
                        yield {
                            "finish_reason": event.get("finishReason", "stop"),
                            "usage": {
                                "prompt_tokens": event.get("inputTokens", 0),
                                "completion_tokens": event.get("outputTokens", 0),
                                "total_tokens": event.get("inputTokens", 0) + event.get("outputTokens", 0),
                            },
                        }
                except json.JSONDecodeError:
                    continue

        if not has_content:
            raise EmptyResponseError("????????? delta ??")


async def relogin_with_curl(email_addr: str, proxy: Optional[str] = None) -> Optional[dict]:
    """
    ??? curl_cffi ???? WorkOS ???????? session/csrf?
    ??????????? Chromium/Playwright??????????? 2G ??????
    """
    proxy_url = proxy or HTTP_PROXY
    es = EmailService()
    name = email_addr.split("@")[0]

    # 1. ???? JWT
    jwt, full_addr = es.recover_email_jwt(name)
    if not jwt:
        log.error(f"[relogin] ?????? JWT: {email_addr}")
        return None

    # ??????????? OTP???????????????
    old_raw = es.fetch_first_email(jwt)
    old_code = None
    if old_raw:
        m = re.findall(r"\b\d{6}\b", old_raw)
        if m:
            old_code = m[0]

    try:
        async with AsyncSession(proxy=proxy_url or None, impersonate=IMPERSONATE) as s:
            # 1. ?? WorkOS ???
            log.info(f"[relogin] 1. ?? WorkOS ????: {email_addr}")
            r1 = await s.get(
                "https://mantle.chat/api/auth/workos/login?provider=authkit&returnUrl=%2Fchat",
                allow_redirects=True,
                timeout=30,
            )
            if r1.status_code >= 400:
                log.error(f"[relogin] WorkOS ???????: HTTP {r1.status_code}")
                return None

            parsed = urlparse(r1.url)
            qs = parse_qs(parsed.query)
            client_id = qs.get("client_id", [""])[0]
            redirect_uri = qs.get("redirect_uri", [""])[0]
            state = qs.get("state", [""])[0]
            session_id = qs.get("authorization_session_id", [""])[0]

            if not session_id or not redirect_uri:
                log.error(f"[relogin] ?? session_id ? redirect_uri: {r1.url}")
                return None

            # 2. ????
            log.info(f"[relogin] 2. ????? AuthKit: {email_addr}")
            mime = CurlMime()
            mime.addpart(name="_1_email", data=email_addr.encode("utf-8"))
            mime.addpart(name="_1_redirect_uri", data=redirect_uri.encode("utf-8"))
            mime.addpart(name="_1_authorization_session_id", data=session_id.encode("utf-8"))
            mime.addpart(name="_1_state", data=state.encode("utf-8"))
            mime.addpart(name="0", data=b'["$K1"]')

            headers_submit = {
                "Next-Action": "409dfdd69397fa9f326db3612977e0aa51bada7cdb",
                "Accept": "text/x-component",
                "Referer": r1.url,
                "Origin": f"{parsed.scheme}://{parsed.netloc}",
            }

            r2 = await s.post(r1.url, multipart=mime, headers=headers_submit, timeout=30)
            redir = r2.headers.get("x-action-redirect", "")
            if not redir:
                log.error(f"[relogin] ??????????: status={r2.status_code}")
                return None

            redir_path = redir.split(";")[0]
            redir_qs = parse_qs(urlparse(redir_path).query)
            challenge_id = redir_qs.get("authentication_challenge_id", [""])[0]
            radar_id = redir_qs.get("radar_auth_attempt_id", [""])[0]
            radar_created_at = redir_qs.get("radar_auth_attempt_created_at", [""])[0]
            magic_state = redir_qs.get("state", [""])[0]

            # 3. ??????
            log.info(f"[relogin] 3. ?? OTP ???: {email_addr}...")
            code = None
            for _ in range(25):
                await asyncio.sleep(2)
                try:
                    raw_mail = es.fetch_first_email(jwt)
                    if raw_mail:
                        m = re.findall(r"\b\d{6}\b", raw_mail)
                        if m and m[0] != old_code:
                            code = m[0]
                            log.info(f"[relogin] ???? OTP ???: {code}")
                            break
                except Exception as e:
                    log.debug(f"[relogin] ??????: {e}")

            if not code:
                log.error(f"[relogin] ?? OTP ?????: {email_addr}")
                return None

            # 4. ?????
            log.info("[relogin] 4. ?? OTP ???...")
            mime_otp = CurlMime()
            mime_otp.addpart(name="_1_code", data=code.encode("utf-8"))
            mime_otp.addpart(name="_1_redirect_uri", data=redirect_uri.encode("utf-8"))
            mime_otp.addpart(name="_1_authorization_session_id", data=session_id.encode("utf-8"))
            mime_otp.addpart(name="_1_state", data=magic_state.encode("utf-8"))
            mime_otp.addpart(name="_1_email", data=email_addr.encode("utf-8"))
            mime_otp.addpart(name="_1_authentication_challenge_id", data=challenge_id.encode("utf-8"))
            mime_otp.addpart(name="_1_radar_auth_attempt_id", data=radar_id.encode("utf-8"))
            mime_otp.addpart(name="_1_radar_auth_attempt_created_at", data=radar_created_at.encode("utf-8"))
            mime_otp.addpart(name="0", data=b'["$K1"]')

            otp_url = f"{parsed.scheme}://{parsed.netloc}{redir_path}"
            headers_otp = {
                "Next-Action": "407aa320ed3a3d9010c11a0a290f46c737a94ddb35",
                "Accept": "text/x-component",
                "Referer": otp_url,
                "Origin": f"{parsed.scheme}://{parsed.netloc}",
            }

            r3 = await s.post(otp_url, multipart=mime_otp, headers=headers_otp, timeout=30)
            if r3.status_code != 200:
                log.error(f"[relogin] ???????: HTTP {r3.status_code}")
                return None

            # 5. ?? clientRedirect
            log.info("[relogin] 5. ?? AuthKit ?????...")
            rsc_target = f"{parsed.scheme}://{parsed.netloc}/success?client_redirect_key={session_id}&_rsc"
            r_rsc = await s.get(rsc_target, headers={"RSC": "1"}, allow_redirects=False, timeout=30)

            match = re.search(r'"clientRedirect":"([^"]+)"', r_rsc.text)
            if not match:
                log.error("[relogin] AuthKit RSC ?????? clientRedirect")
                return None

            client_redirect_url = match.group(1).encode("utf-8").decode("unicode_escape")

            # 6. ?????????? Session Cookie
            log.info("[relogin] 6. Mantle ????...")
            r_cb = await s.get(client_redirect_url, allow_redirects=True, timeout=30)
            if r_cb.status_code >= 400:
                log.error(f"[relogin] Mantle ????: HTTP {r_cb.status_code}")
                return None

            mantle_cookies = {}
            for c in s.cookies.jar:
                if "mantle.chat" in getattr(c, "domain", ""):
                    mantle_cookies[c.name] = c.value

            # ?? csrf_token
            r_csrf = await s.get(f"{MANTLE_API}/auth/csrf", timeout=15)
            csrf_token = r_csrf.json().get("csrfToken", "") if r_csrf.status_code == 200 else ""
            if not csrf_token:
                csrf_token = mantle_cookies.get("mantle-csrf-token", "")

            log.info(f"[relogin] ??????! Email={email_addr}")
            return {
                "cookies": mantle_cookies,
                "csrf_token": csrf_token,
            }
    except Exception as e:
        log.error(f"[relogin] ?????????: {email_addr} - {e}", exc_info=True)
        return None


# ????
relogin_with_browser = relogin_with_curl
