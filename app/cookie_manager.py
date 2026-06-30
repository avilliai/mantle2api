# app/cookie_manager.py

import asyncio
import json
import logging
import os
import threading
from typing import Optional

from fastapi import HTTPException

from app.config import TOKENS_FILE
from app.models import AccountState

log = logging.getLogger("cosverse_proxy.cookie_manager")

ACCOUNT_MAX_USAGE = 100
INITIAL_POOL_SIZE = 10
REPLENISH_SIZE = 3
LOW_WATERMARK = 1


class CookieManager:
    def __init__(self):
        self.accounts: list[AccountState] = []
        self._idx: int = 0
        self._async_lock = asyncio.Lock()  # 保护写操作
        self._idx_lock = asyncio.Lock()  # 保护轮询
        self._sync_lock = threading.Lock()  # 保护本地内存状态

        self.initial_done = False
        self._maintain_lock = asyncio.Lock()  # 保护账号池水位维护

    # ── 持久化 ────────────────────────────────────────────
    def load(self) -> None:
        if not TOKENS_FILE.exists():
            log.warning(f"未找到 {TOKENS_FILE}，已初始化空列表")
            self.save()
            return
        try:
            data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
            with self._sync_lock:
                accounts = []
                for i, d in enumerate(data):
                    if not d.get("cookie_str"):
                        continue
                    acc = AccountState(d, i + 1)
                    if d.get("email_jwt"):
                        acc.email_jwt = d["email_jwt"]
                    if d.get("reset_at"):
                        acc.reset_at = d["reset_at"]
                    accounts.append(acc)
                self.accounts = accounts
            log.info(f"✅ 成功加载账号: {len(self.accounts)} 个")
        except Exception as e:
            log.error(f"解析 tokens 失败: {e}")

    def save(self) -> None:
        with self._sync_lock:
            try:
                raw = []
                for acc in self.accounts:
                    d = acc.to_dict()
                    email_jwt = getattr(acc, "email_jwt", "")
                    if email_jwt:
                        d["email_jwt"] = email_jwt
                    reset_at = getattr(acc, "reset_at", 0)
                    if reset_at:
                        d["reset_at"] = reset_at
                    raw.append(d)
                TOKENS_FILE.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=4), encoding="utf-8"
                )
            except Exception as e:
                log.warning(f"保存 tokens 失败: {e}")

    async def async_save(self) -> None:
        await asyncio.to_thread(self.save)

    # ── 提供给管理后台接口的数据 ──────────────────────────
    def get_tokens_detail(self) -> dict:
        import time
        tokens_list = []
        total_remaining = 0
        alive_count = 0
        now = time.time()

        with self._sync_lock:
            for acc in self.accounts:
                reset_at = getattr(acc, "reset_at", 0) or 0
                effective_usage = acc.usage_count if (now - reset_at < 86400) else 0
                remaining = max(0, ACCOUNT_MAX_USAGE - effective_usage)
                total_remaining += remaining

                if acc.healthy and not acc.disabled and remaining > 0:
                    alive_count += 1

                next_reset_in = max(0, int(86400 - (now - reset_at))) if reset_at else None

                tokens_list.append({
                    "index": acc.index,
                    "email": acc.email,
                    "token_hint": getattr(acc, "token_hint", acc.email),
                    "is_alive": acc.healthy,
                    "disabled": acc.disabled,
                    "remaining": remaining,
                    "next_reset_in": next_reset_in,
                })

        return {
            "tokens": tokens_list,
            "summary": {
                "total": len(self.accounts),
                "alive": alive_count,
                "total_remaining": total_remaining,
                "last_refresh": None,
            },
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
                    if data.get("email_jwt"):
                        acc.email_jwt = data["email_jwt"]
                    return False
            new_idx = max([a.index for a in self.accounts] + [0]) + 1
            self.accounts.append(AccountState(data, new_idx))
        return True

    # ── 注册新账号 ────────────────────────────────────────
    async def _register_new_account(self) -> Optional[AccountState]:
        try:
            from app.cosverse_client import CosverseRegister
            log.info("🔄 开始注册全新账号...")
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

    # ── 单个账号保活（唤醒）───────────────────────────────
    async def refresh_account(self, acc: AccountState) -> bool:
        from app.cosverse_client import CosverseRefresher
        log.info(f"🔄 开始尝试唤醒(保活): {acc.email}")
        ok = await CosverseRefresher(acc).run()
        if ok:
            await self.async_save()
            log.info(f"✅ 唤醒成功并已持久化: {acc.email}")
        else:
            log.warning(f"❌ 唤醒失败: {acc.email}")
        return ok

    # ── 已废弃的批量保活（仅留空实现兼容旧后台路由） ───────
    async def refresh_all_unhealthy(self) -> dict:
        log.warning("拦截调用: 为防范风控，批量保活机制已被抛弃")
        return {"success": [], "failed": [], "msg": "Batch refresh abandoned to avoid risk control."}

    # ── 动态监测与补充机制 (核心) ─────────────────────────
    def _get_valid_accounts(self) -> list[AccountState]:
        """获取当前配额未满、未被禁用且存活的账号列表"""
        for a in self.accounts:
            self._maybe_reset_usage(a)
        return [
            a for a in self.accounts
            if a.healthy and not a.disabled and a.usage_count < ACCOUNT_MAX_USAGE
        ]

    async def maintain_pool(self) -> None:
        """
        根据实际用量维护账号池。
        1. 初始期间维持 INITIAL_POOL_SIZE 个账号；
        2. 当使用耗尽（降至 LOW_WATERMARK 以下）时，仅引入 REPLENISH_SIZE 个账号。
        """
        if self._maintain_lock.locked():
            return

        async with self._maintain_lock:
            valid_count = len(self._get_valid_accounts())

            if not self.initial_done:
                if valid_count < INITIAL_POOL_SIZE:
                    needed = INITIAL_POOL_SIZE - valid_count
                    log.info(f"🔄 启动初始化：账号池需补充 {needed} 个 (目标维持 {INITIAL_POOL_SIZE} 个)")
                    await self._replenish(needed)
                self.initial_done = True
            else:
                if valid_count <= LOW_WATERMARK:
                    log.info(
                        f"⚠️ 触发监测：当前存活配额耗尽 ({valid_count}/{LOW_WATERMARK})，将按需引入 {REPLENISH_SIZE} 个账号")
                    await self._replenish(REPLENISH_SIZE)

    async def _replenish(self, count: int) -> None:
        """从历史账号中唤醒或注册指定数量的账号，避免高频并发风控"""
        attempted_indices = set()
        for i in range(count):
            candidate = None
            with self._sync_lock:
                for a in self.accounts:
                    # 寻找休眠账号：未因超额被禁用，但因长期未用或过期导致 healthy=False
                    if not a.healthy and not a.disabled and a.usage_count < ACCOUNT_MAX_USAGE and a.index not in attempted_indices:
                        candidate = a
                        break

            if candidate:
                attempted_indices.add(candidate.index)
                candidate.disabled = False  # 防御性解禁
                ok = await self.refresh_account(candidate)
                if ok:
                    if i < count - 1: await asyncio.sleep(5)  # 错峰延迟防风控
                    continue
                log.warning(f"账号 {candidate.email} 唤醒失败，将转为注册新账号")

            # 无休眠账号可用或唤醒失败，注册全新号
            await self._register_new_account()

            if i < count - 1:
                await asyncio.sleep(5)

    # ── 轮询获取可用账号 ──────────────────────────────────
    async def get_next(self) -> AccountState:
        valid = self._get_valid_accounts()

        # 水位偏低，后台静默补充，避免阻塞当前用户
        if len(valid) <= LOW_WATERMARK:
            asyncio.create_task(self.maintain_pool())

        if not valid:
            log.warning("⚠️ 彻底无可用账号，同步等待补充任务完成...")
            async with self._maintain_lock:
                valid = self._get_valid_accounts()
                if not valid:
                    await self._replenish(REPLENISH_SIZE)
                    valid = self._get_valid_accounts()

            if not valid:
                raise HTTPException(500, "当前无可用账号且按需自动引入失败")

        async with self._idx_lock:
            acc = valid[self._idx % len(valid)]
            self._idx += 1

        return acc

    # ── 换号与失败重试逻辑 ────────────────────────────────
    async def mark_failed_and_get_replacement(
            self,
            acc: AccountState,
            reason: str = "unknown",
            attempt: int = 1,
    ) -> AccountState:
        """
        遇到对话故障时：
        - 仅标记 healthy=False，使其变为休眠状态进入唤醒候选。
        - 若连续失败到达第 3 次，判定为遇到风控墙，主动触发引入3个全新/干净账号。
        """
        async with self._async_lock:
            acc.healthy = False
            log.warning(f"❌ 账号 [{acc.email}] 调用异常 (被封或过期)，暂时隔离，原因: {reason} (第{attempt}次重试)")
            self.save()

        if attempt < 3:
            valid = self._get_valid_accounts()
            valid = [a for a in valid if a.index != acc.index]
            if valid:
                log.info(f"🔄 切换到存活池备用账号: {valid[0].email}")
                return valid[0]
            log.warning("⚠️ 备用池已空，立即触发补充...")

        # 连续失败或池子彻底枯竭，果断引入纯新批次
        log.warning(f"⚠️ 第 {attempt} 次重试或池子空，主动引入 {REPLENISH_SIZE} 个新批次账号...")
        async with self._maintain_lock:
            await self._replenish(REPLENISH_SIZE)
            valid = self._get_valid_accounts()

        if valid:
            return valid[0]

        raise HTTPException(500, "换号重试耗尽：引入新批次后仍无可用")

    # ── 用量计费与 24h 配额恢复 ───────────────────────────
    def _maybe_reset_usage(self, acc: AccountState) -> None:
        import time
        now = time.time()
        reset_at = getattr(acc, "reset_at", 0) or 0
        if now - reset_at >= 86400:
            if acc.usage_count > 0 or acc.disabled:
                log.info(
                    f"[{acc.email}] ♻️ 24h 配额重置, usage_count {acc.usage_count} → 0"
                )
            acc.usage_count = 0
            acc.reset_at = now
            if acc.disabled:
                # 满 24 小时后，移除禁用标记，使其可被重新唤醒
                acc.disabled = False
                log.info(f"[{acc.email}] ♻️ 额度限制解除，已恢复到休眠池等待使用")

    def record_usage(self, acc: AccountState) -> None:
        self._maybe_reset_usage(acc)

        if hasattr(acc, "increment_usage"):
            acc.increment_usage()
        else:
            acc.usage_count += 1

        if acc.usage_count >= ACCOUNT_MAX_USAGE:
            acc.disabled = True
            log.info(f"🚫 [{acc.email}] 当日额度耗尽 {ACCOUNT_MAX_USAGE}，自动禁用等待重置。")

        self.save()
        log.info(f"📈 [{acc.email}] 已用配额: {acc.usage_count}/{ACCOUNT_MAX_USAGE}")


# 全局单例
cm = CookieManager()