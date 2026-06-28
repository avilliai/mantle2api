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
ACCOUNT_MAX_USAGE = 100


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

    # ── 提供给管理后台接口的数据 ──────────────────────────
    def get_tokens_detail(self) -> dict:
        tokens_list = []
        total_remaining = 0
        alive_count = 0

        with self._sync_lock:
            for acc in self.accounts:
                remaining = max(0, ACCOUNT_MAX_USAGE - acc.usage_count)
                total_remaining += remaining

                if acc.healthy and not acc.disabled and remaining > 0:
                    alive_count += 1

                tokens_list.append({
                    "index":      acc.index,
                    "email":      acc.email,
                    "token_hint": getattr(acc, "token_hint", acc.email),
                    "is_alive":   acc.healthy,
                    "disabled":   acc.disabled,
                    "remaining":  remaining,
                })

        return {
            "tokens": tokens_list,
            "summary": {
                "total":           len(self.accounts),
                "alive":           alive_count,
                "total_remaining": total_remaining,
                "last_refresh":    None,
            },
        }

    # ── 账号增删 ──────────────────────────────────────────
    def add_account(self, data: dict) -> bool:
        with self._sync_lock:
            for acc in self.accounts:
                if acc.email == data.get("email"):
                    acc.cookie_str  = data.get("cookie_str", acc.cookie_str)
                    acc.name        = data.get("name", acc.name)
                    acc.user_id     = data.get("user_id", acc.user_id)
                    acc.healthy     = True
                    acc.usage_count = data.get("usage_count", acc.usage_count)
                    # 更新 email_jwt（如果注册时带回来了）
                    if data.get("email_jwt"):
                        acc.email_jwt = data["email_jwt"]
                    return False
            new_idx = max([a.index for a in self.accounts] + [0]) + 1
            self.accounts.append(AccountState(data, new_idx))
        return True

    # ── 后台注册新账号 ────────────────────────────────────
    async def _register_new_account(self) -> Optional[AccountState]:
        try:
            from app.cosverse_client import CosverseRegister
            log.info("🔄 开始注册新账号...")
            result = await asyncio.wait_for(CosverseRegister().run(), timeout=180)
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

    # ── 保活：单个账号 ────────────────────────────────────
    async def refresh_account(self, acc: AccountState) -> bool:
        """
        对指定账号执行保活（重新登录拿新 cookie）。
        成功后持久化到磁盘。
        """
        from app.cosverse_client import CosverseRefresher
        log.info(f"🔄 开始保活: {acc.email}")
        ok = await CosverseRefresher(acc).run()
        if ok:
            await self.async_save()
            log.info(f"✅ 保活成功并已持久化: {acc.email}")
        else:
            log.warning(f"❌ 保活失败: {acc.email}")
        return ok

    # ── 保活：批量刷新所有不健康账号 ─────────────────────
    async def refresh_all_unhealthy(self) -> dict:
        """
        批量保活所有 healthy=False 或 disabled=True 的账号。
        返回 {"success": [...], "failed": [...]} 统计结果。
        """
        with self._sync_lock:
            targets = [
                a for a in self.accounts
                if not a.healthy or a.disabled
            ]

        log.info(f"🔄 批量保活，目标账号数: {len(targets)}")
        success, failed = [], []

        for acc in targets:
            # 临时解禁，让 Refresher 可以正常写回
            acc.disabled = False
            ok = await self.refresh_account(acc)
            (success if ok else failed).append(acc.email)
            await asyncio.sleep(2)   # 错峰，避免短时间内大量请求

        log.info(f"批量保活完成：成功 {len(success)}，失败 {len(failed)}")
        return {"success": success, "failed": failed}

    # ── 定时保活（供 main.py 后台任务调用）───────────────
    async def keepalive_tick(self) -> None:
        """
        每次定时触发时调用：
        1. 先检查所有账号的 session 是否仍然有效
        2. 对 session 已失效的账号触发保活（重新登录）
        3. 检查备用账号是否充足
        """
        log.info("⏰ keepalive_tick 开始")

        # 第一步：session 检查
        with self._sync_lock:
            candidates = [
                a for a in self.accounts
                if not a.disabled and a.usage_count < ACCOUNT_MAX_USAGE
            ]

        for acc in candidates:
            alive = await self.check_session(acc)
            if not alive:
                log.warning(f"[{acc.email}] session 已失效，触发保活...")
                await self.refresh_account(acc)
            await asyncio.sleep(3)   # 错峰

        # 第二步：确保备用账号充足
        await self._ensure_spare_accounts()
        log.info("⏰ keepalive_tick 完成")

    # ── 轮询获取可用账号 ──────────────────────────────────
    async def get_next(self) -> AccountState:
        async with self._async_lock:
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

    # ── 换号重试逻辑 ──────────────────────────────────────
    async def mark_failed_and_get_replacement(
            self,
            acc: AccountState,
            reason: str = "unknown",
            attempt: int = 1,
    ) -> AccountState:
        """
        账号调用失败时调用。
        - 前 2 次：从号池找备用号；
        - 第 3 次：先尝试对该账号保活，保活失败再注册新号。
        """
        async with self._async_lock:
            acc.healthy  = False
            acc.disabled = True
            log.warning(f"❌ 账号 [{acc.email}] 故障已禁用，原因: {reason} (第{attempt}次重试)")
            self.save()

            if attempt < 3:
                valid = [
                    a for a in self.accounts
                    if a.healthy and not a.disabled
                    and a.usage_count < ACCOUNT_MAX_USAGE
                    and a.index != acc.index
                ]
                if valid:
                    log.info(f"🔄 切换到备用账号: {valid[0].email}")
                    return valid[0]
                log.warning("⚠️ 备用池已空，将保活或注册新账号...")

        # attempt >= 3 或备用池为空：先试保活，失败再注册
        if attempt >= 3:
            log.warning(f"⚠️ 第 {attempt} 次重试，先尝试保活 [{acc.email}]...")
            acc.disabled = False   # 解禁才能保活
            ok = await self.refresh_account(acc)
            if ok:
                log.info(f"✅ 保活成功，继续使用 [{acc.email}]")
                return acc
            log.warning(f"保活失败，改为注册新账号...")

        new_acc = await self._register_new_account()
        if new_acc:
            return new_acc

        raise HTTPException(500, "换号重试失败：无备用账号且自动注册/保活均未成功")

    # ── 用量记账 ──────────────────────────────────────────
    def record_usage(self, acc: AccountState) -> None:
        if hasattr(acc, "increment_usage"):
            acc.increment_usage()
        else:
            acc.usage_count += 1

        if acc.usage_count >= ACCOUNT_MAX_USAGE:
            acc.disabled = True
            log.info(f"🚫 [{acc.email}] 额度已达 {ACCOUNT_MAX_USAGE}，自动禁用。")

        self.save()
        log.info(f"📈 [{acc.email}] 已用: {acc.usage_count}/{ACCOUNT_MAX_USAGE}")

    # ── 账号 session 检查 ─────────────────────────────────
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