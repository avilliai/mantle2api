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

PRE_REGISTER_THRESHOLD = 2
ACCOUNT_MAX_USAGE = 100


class CookieManager:
    def __init__(self):
        self.accounts: list[AccountState] = []
        self._idx: int = 0
        self._async_lock = asyncio.Lock()   # 仅保护换号/注册等写操作
        self._idx_lock   = asyncio.Lock()   # 仅保护 _idx 轮询，极短临界区
        self._sync_lock  = threading.Lock()
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
                accounts = []
                for i, d in enumerate(data):
                    if not d.get("cookie_str"):
                        continue
                    acc = AccountState(d, i + 1)
                    # 恢复 email_jwt（保活时收 OTP 必须）
                    if d.get("email_jwt"):
                        acc.email_jwt = d["email_jwt"]
                    # 恢复 reset_at（24h 配额重置时间戳）
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
                    # email_jwt는 to_dict()에 없을 수 있으므로 명시적으로 추가
                    email_jwt = getattr(acc, "email_jwt", "")
                    if email_jwt:
                        d["email_jwt"] = email_jwt
                    # reset_at：24h 配额重置时间戳
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
                # 计算剩余量时考虑 24h 重置：若已过 24h，usage 视为 0
                reset_at  = getattr(acc, "reset_at", 0) or 0
                effective_usage = acc.usage_count if (now - reset_at < 86400) else 0
                remaining = max(0, ACCOUNT_MAX_USAGE - effective_usage)
                total_remaining += remaining

                if acc.healthy and not acc.disabled and remaining > 0:
                    alive_count += 1

                next_reset_in = max(0, int(86400 - (now - reset_at))) if reset_at else None

                tokens_list.append({
                    "index":         acc.index,
                    "email":         acc.email,
                    "token_hint":    getattr(acc, "token_hint", acc.email),
                    "is_alive":      acc.healthy,
                    "disabled":      acc.disabled,
                    "remaining":     remaining,
                    "next_reset_in": next_reset_in,   # 距下次重置的秒数
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
    KEEPALIVE_CONCURRENCY = int(os.environ.get("KEEPALIVE_CONCURRENCY", "20"))

    async def keepalive_tick(self) -> None:
        """
        每次定时触发时调用：
        1. 对所有未禁用且未超额的账号并行做 LLM 探活（check_session）
        2. 仅对探活失败的账号执行 refresh（重新登录拿新 cookie）
        3. 检查备用账号是否充足

        全程不持有 _async_lock / _idx_lock，保活期间 chat 请求完全不受阻塞。
        """
        log.info("⏰ keepalive_tick 开始")

        # 快照候选列表（只读 _sync_lock，极短）
        with self._sync_lock:
            candidates = [
                a for a in self.accounts
                if not a.disabled and a.usage_count < ACCOUNT_MAX_USAGE
            ]

        if not candidates:
            log.info("⏰ 无需保活的候选账号，跳过。")
            await self._ensure_spare_accounts()
            log.info("⏰ keepalive_tick 完成")
            return

        log.info(f"⏰ 开始并行探活，目标账号数: {len(candidates)}，并发度: {self.KEEPALIVE_CONCURRENCY}")

        sem = asyncio.Semaphore(self.KEEPALIVE_CONCURRENCY)
        probe_counter = {"alive": 0, "dead": 0}
        dead_accounts: list[AccountState] = []

        async def _bounded_probe(acc):
            async with sem:
                # save=False：只更新内存中的 acc.healthy，全部探完后统一写盘
                alive = await self.check_session(acc, save=False)
                if alive:
                    probe_counter["alive"] += 1
                else:
                    probe_counter["dead"] += 1
                    dead_accounts.append(acc)

        await asyncio.gather(*[_bounded_probe(a) for a in candidates])

        # 探活结果统一写盘一次，而非每个账号各写一次
        await self.async_save()

        log.info(
            f"⏰ 探活完成：存活 {probe_counter['alive']}，"
            f"需刷新 {probe_counter['dead']}，共 {len(candidates)} 个"
        )

        # 仅对探活失败的账号执行 refresh（refresh 内部自己会 save）
        if dead_accounts:
            log.info(f"⏰ 开始对 {len(dead_accounts)} 个失效账号执行 refresh...")
            refresh_counter = {"ok": 0, "fail": 0}

            async def _bounded_refresh(acc):
                async with sem:
                    ok = await self.refresh_account(acc)
                    if ok:
                        refresh_counter["ok"] += 1
                    else:
                        refresh_counter["fail"] += 1

            await asyncio.gather(*[_bounded_refresh(a) for a in dead_accounts])
            log.info(
                f"⏰ refresh 完成：成功 {refresh_counter['ok']}，"
                f"失败 {refresh_counter['fail']}"
            )
        else:
            log.info("⏰ 所有账号均存活，无需 refresh。")

        await self._ensure_spare_accounts()
        log.info("⏰ keepalive_tick 完成")

    # ── 轮询获取可用账号 ──────────────────────────────────
    async def get_next(self) -> AccountState:
        # 24h 配额重置检查（无锁，属性赋值原子性由 GIL 保证）
        for a in self.accounts:
            self._maybe_reset_usage(a)

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

        # 只在轮询取号这一瞬间持锁，不阻塞保活
        async with self._idx_lock:
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
    def _maybe_reset_usage(self, acc: AccountState) -> None:
        """
        如果距离上次重置已超过 24h，将 usage_count 归零并重新启用账号。
        reset_at 字段记录上次重置的 Unix 时间戳（秒），持久化在 tokens.json 中。
        """
        import time
        now = time.time()
        reset_at = getattr(acc, "reset_at", 0) or 0
        if now - reset_at >= 86400:  # 24 小时 = 86400 秒
            if acc.usage_count > 0 or acc.disabled:
                log.info(
                    f"[{acc.email}] ♻️  24h 配额重置 "
                    f"(距上次重置 {int((now - reset_at) / 3600)}h)，"
                    f"usage_count {acc.usage_count} → 0"
                )
            acc.usage_count = 0
            acc.reset_at    = now
            # 仅因额度耗尽而被禁用的账号（healthy=True），重置后自动解禁
            if acc.disabled and acc.healthy:
                acc.disabled = False
                log.info(f"[{acc.email}] ♻️  额度已重置，自动解禁")

    def record_usage(self, acc: AccountState) -> None:
        # 先检查是否需要重置 24h 配额
        self._maybe_reset_usage(acc)

        if hasattr(acc, "increment_usage"):
            acc.increment_usage()
        else:
            acc.usage_count += 1

        if acc.usage_count >= ACCOUNT_MAX_USAGE:
            acc.disabled = True
            log.info(f"🚫 [{acc.email}] 额度已达 {ACCOUNT_MAX_USAGE}，自动禁用。")

        self.save()
        log.info(f"📈 [{acc.email}] 已用: {acc.usage_count}/{ACCOUNT_MAX_USAGE}")

    # ── 账号 session 检查（LLM 探活） ────────────────────
    async def check_session(self, acc: AccountState, save: bool = True) -> bool:
        """
        通过发起一次轻量 LLM 对话来判断 session 是否仍然有效。
        - 正常返回非空文本 → 存活，返回 True
        - 返回空响应（EmptyResponseError）或 401 → 已过期，返回 False
        - 其他网络异常 → 保守视为存活（True），避免误杀
        save=False 时跳过持久化，供 keepalive_tick 批量探活后统一保存。
        """
        from app.cosverse_client import stream_cosverse_response, EmptyResponseError
        try:
            chunks = []
            async for chunk in stream_cosverse_response(
                text="Hi",
                acc=acc,
                model="google/gemma-4-31b-it:free",
                provider="google",
            ):
                chunks.append(chunk)
                # 拿到第一个非空 chunk 即可确认存活，提前退出
                break

            alive = len(chunks) > 0
            acc.healthy = alive
            if save:
                self.save()
            if alive:
                log.info(f"[{acc.email}] LLM 探活：存活 ✅")
            else:
                log.warning(f"[{acc.email}] LLM 探活：空响应，视为过期 ❌")
            return alive

        except EmptyResponseError:
            log.warning(f"[{acc.email}] LLM 探活：EmptyResponseError，视为过期 ❌")
            acc.healthy = False
            if save:
                self.save()
            return False

        except PermissionError:
            log.warning(f"[{acc.email}] LLM 探活：401 cookie 过期 ❌")
            acc.healthy = False
            if save:
                self.save()
            return False

        except Exception as e:
            if "429" in str(e):
                return False
            # 网络抖动等瞬时错误，不确定是否过期，保守认为存活
            log.warning(f"[{acc.email}] LLM 探活：异常（{e}），保守视为存活")
            return True


# 全局单例
cm = CookieManager()