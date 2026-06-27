# app/cookie_manager.py

import asyncio
import json
import logging
import threading
from typing import Optional

from fastapi import HTTPException

from app.config import TOKENS_FILE
from app.models import AccountState

log = logging.getLogger("cosverse_proxy.cookie_manager")

PRE_REGISTER_THRESHOLD = 2
ACCOUNT_MAX_USAGE = 100  # 强制限制最大使用次数


class CookieManager:
    def __init__(self):
        self.accounts: list[AccountState] = []
        self._idx: int = 0
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()
        self._registering = False

    # ── 持久化 ────────────────────────────────────────────
    def load(self) -> None:
        if not TOKENS_FILE.exists():
            log.warning(f"未找到 {TOKENS_FILE}，已初始化空列表")
            self.save()
            return
        try:
            data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
            with self._sync_lock:
                self.accounts = [
                    AccountState(d, i + 1)
                    for i, d in enumerate(data)
                    if d.get("cookie_str")
                ]
            log.info(f"✅ 成功加载账号: {len(self.accounts)} 个")
        except Exception as e:
            log.error(f"解析 tokens 失败: {e}")

    def save(self) -> None:
        with self._sync_lock:
            try:
                raw = [acc.to_dict() for acc in self.accounts]
                TOKENS_FILE.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=4), encoding="utf-8"
                )
            except Exception as e:
                log.warning(f"保存 tokens 失败: {e}")

    async def async_save(self) -> None:
        await asyncio.to_thread(self.save)

    # ── 提供给管理后台接口的数据（对接前端 Dashboard） ───────────
    def get_tokens_detail(self) -> dict:
        tokens_list = []
        total_remaining = 0
        alive_count = 0

        with self._sync_lock:
            for acc in self.accounts:
                # 动态计算真实剩余额度
                remaining = max(0, ACCOUNT_MAX_USAGE - acc.usage_count)
                total_remaining += remaining

                if acc.healthy and not acc.disabled and remaining > 0:
                    alive_count += 1

                tokens_list.append({
                    "index": acc.index,
                    "email": acc.email,
                    "token_hint": getattr(acc, "token_hint", acc.email),
                    "is_alive": acc.healthy,
                    "disabled": acc.disabled,
                    "remaining": remaining,  # 确保前端正确显示
                })

        return {
            "tokens": tokens_list,
            "summary": {
                "total": len(self.accounts),
                "alive": alive_count,
                "total_remaining": total_remaining,
                "last_refresh": None
            }
        }

    # ── 账号增删 ──────────────────────────────────────────
    def add_account(self, data: dict) -> bool:
        with self._sync_lock:
            for acc in self.accounts:
                if acc.email == data.get("email"):
                    acc.cookie_str = data.get("cookie_str", acc.cookie_str)
                    acc.name = data.get("name", acc.name)
                    acc.user_id = data.get("user_id", acc.user_id)
                    acc.healthy = True
                    acc.usage_count = data.get("usage_count", acc.usage_count)
                    return False
            new_idx = max([a.index for a in self.accounts] + [0]) + 1
            self.accounts.append(AccountState(data, new_idx))
        return True

    # ── 后台注册新账号 ────────────────────────────────────
    async def _register_new_account(self) -> Optional[AccountState]:
        try:
            import asyncio as _asyncio
            from app.cosverse_client import CosverseRegister
            log.info("🔄 开始注册新账号...")
            result = await _asyncio.wait_for(CosverseRegister().run(), timeout=180)
            if result:
                self.add_account(result)
                self.save()
                log.info(f"🎉 注册成功: {result['email']}")
                with self._sync_lock:
                    new_acc = next(
                        (a for a in self.accounts if a.email == result["email"]), None
                    )
                return new_acc
            log.warning("💥 注册失败：CosverseRegister.run() 返回 None")
        except Exception as e:
            log.error(f"注册异常: {e}")
        return None

    async def force_register_new_account(self) -> Optional[AccountState]:
        return await self._register_new_account()

    async def _ensure_spare_accounts(self) -> None:
        if self._registering:
            return
        valid_count = sum(
            1 for a in self.accounts
            if a.healthy and not a.disabled and a.usage_count < ACCOUNT_MAX_USAGE
        )
        if valid_count < PRE_REGISTER_THRESHOLD:
            self._registering = True
            asyncio.create_task(self._bg_register())

    async def _bg_register(self) -> None:
        try:
            await self._register_new_account()
        finally:
            self._registering = False

    # ── 轮询获取可用账号 ──────────────────────────────────
    async def get_next(self) -> AccountState:
        async with self._async_lock:
            # 严格限制：健康、未禁用、使用次数 < 100
            valid = [
                a for a in self.accounts
                if a.healthy and not a.disabled and a.usage_count < ACCOUNT_MAX_USAGE
            ]
            if not valid:
                log.warning("⚠️ 无可用账号，同步等待注册一个...")
                acc = await self._register_new_account()
                if acc:
                    asyncio.create_task(self._ensure_spare_accounts())
                    return acc
                raise HTTPException(500, "无可用账号且自动注册失败")

            acc = valid[self._idx % len(valid)]
            self._idx += 1

        asyncio.create_task(self._ensure_spare_accounts())
        return acc

    # ── 换号重试逻辑（第3次强制注册） ──────────────────────
    async def mark_failed_and_get_replacement(
            self,
            acc: AccountState,
            reason: str = "unknown",
            attempt: int = 1
    ) -> AccountState:
        """
        当某个账号调用失败（如 cookie 过期），将其标记为不健康并禁用。
        如果前2次失败，优先从号池找备用号；
        如果是第3次（attempt >= 3），直接触发注册新号返回。
        """
        async with self._async_lock:
            acc.healthy = False
            acc.disabled = True
            log.warning(f"❌ 账号 [{acc.email}] 发生故障已禁用，原因: {reason} (第{attempt}次重试)")
            self.save()

            # 如果还没到第3次，优先从现有健康账号里挑一个替补
            if attempt < 3:
                valid = [
                    a for a in self.accounts
                    if a.healthy and not a.disabled and a.usage_count < ACCOUNT_MAX_USAGE
                       and a.index != acc.index
                ]
                if valid:
                    log.info(f"🔄 切换到现有备用账号: {valid[0].email}")
                    return valid[0]
                else:
                    log.warning("⚠️ 备用池已空，将自动注册新账号...")

        # 走到这里只有两种情况：
        # 1. attempt >= 3 （达到第3次重试，根据你的需求强制注册）
        # 2. attempt < 3 但是号池里真的没有其他可用号了
        if attempt >= 3:
            log.warning(f"⚠️ 当前是第 {attempt} 次重试，触发强制注册新账号机制...")

        new_acc = await self._register_new_account()
        if new_acc:
            return new_acc

        raise HTTPException(500, "换号重试失败：无备用账号且自动注册新号未成功")

    # ── 用量记账 ──────────────────────────────────────────
    def record_usage(self, acc: AccountState) -> None:
        """每次成功对话后调用，记录用量并立刻持久化，防止丢失"""
        if hasattr(acc, 'increment_usage'):
            acc.increment_usage()
        else:
            acc.usage_count += 1

        # 到达 100 时直接禁用账号
        if acc.usage_count >= ACCOUNT_MAX_USAGE:
            acc.disabled = True
            log.info(f"🚫 [{acc.email}] 额度已达 {ACCOUNT_MAX_USAGE}，自动禁用不再使用。")

        self.save()
        log.info(f"📈 [{acc.email}] 额度扣减成功，当前已用: {acc.usage_count}/{ACCOUNT_MAX_USAGE}")

    # ── 账号健康检查（简单验证 session） ─────────────────
    async def check_session(self, acc: AccountState) -> bool:
        try:
            from curl_cffi.requests import AsyncSession
            from app.config import COSVERSE_API, COMMON_HEADERS
            async with AsyncSession(impersonate="chrome110", timeout=15) as session:
                resp = await session.get(
                    f"{COSVERSE_API}/auth/session",
                    headers={**COMMON_HEADERS, "accept": "*/*", "content-type": "application/json"},
                    cookies=acc.cookies_dict(),
                )
                data = resp.json()
                if data.get("user", {}).get("id"):
                    acc.healthy = True
                    self.save()
                    return True
                acc.healthy = False
                self.save()
                return False
        except Exception as e:
            log.error(f"[{acc.email}] session check 失败: {e}")
            acc.healthy = False
            self.save()
            return False


# 全局单例
cm = CookieManager()