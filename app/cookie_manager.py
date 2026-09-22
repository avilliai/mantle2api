# app/cookie_manager.py

import asyncio
import datetime
import json
import logging
import os
import threading
from typing import Optional

import dotenv
from fastapi import HTTPException

from app.config import TOKENS_FILE
from app.models import AccountState, ACCOUNT_QUOTA
from app.usage_client import fetch_mantle_usage

log = logging.getLogger("mantle_proxy.cookie_manager")

dotenv.load_dotenv()

FAIL_THRESHOLD = int(os.environ.get("ACCOUNT_FAIL_THRESHOLD", "3"))


class CookieManager:
    def __init__(self):
        self.accounts: list[AccountState] = []
        self._idx: int = 0
        self._idx_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()
        self.last_refresh: Optional[str] = None

    def load(self) -> None:
        if not TOKENS_FILE.exists():
            log.warning(f"未找到 {TOKENS_FILE}，已初始化空列表")
            self.save()
            return
        try:
            raw_text = TOKENS_FILE.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            with self._sync_lock:
                accounts = []
                for i, d in enumerate(data):
                    acc = AccountState(d, i + 1)
                    accounts.append(acc)
                self.accounts = accounts
            log.info(f"成功加载 Mantle 账号: {len(self.accounts)} 个")
        except Exception as e:
            log.error(f"加载 {TOKENS_FILE} 失败: {e}", exc_info=True)

    def save(self) -> None:
        with self._sync_lock:
            data = [a.to_dict() for a in self.accounts]
            TOKENS_FILE.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    async def async_save(self) -> None:
        await asyncio.to_thread(self.save)

    def _find_by_index(self, idx: int) -> Optional[AccountState]:
        return next((a for a in self.accounts if a.index == idx), None)

    @staticmethod
    def _account_is_eligible(acc: Optional[AccountState]) -> bool:
        """
        账号必须健康、未被禁用、且月度配额未耗尽
        """
        if not acc:
            return False
        return acc.healthy and not acc.disabled and not acc.quota_exhausted

    async def get_account(self) -> AccountState:
        """
        在可用账号中轮询负载均衡，严格跳过配额已耗尽、已禁用或不健康的账号。
        不删除旧账号（配额为月度刷新机制）。
        """
        with self._sync_lock:
            eligible_accounts = [a for a in self.accounts if self._account_is_eligible(a)]

        if not eligible_accounts:
            raise HTTPException(
                status_code=503,
                detail="当前无可用 Mantle 账号（所有账号可能已禁用、健康异常或月度配额已耗尽）",
            )

        async with self._idx_lock:
            self._idx = (self._idx + 1) % len(eligible_accounts)
            return eligible_accounts[self._idx]

    def add_account(self, data: dict) -> AccountState:
        with self._sync_lock:
            idx = len(self.accounts) + 1
            acc = AccountState(data, idx)
            self.accounts.append(acc)
        self.save()
        log.info(f"新 Mantle 账号已加入 (index={idx}, email={acc.email})")
        return acc

    async def check_session(self, acc: AccountState, save: bool = True) -> bool:
        """
        通过 mantle usage-status 接口同步真实配额与保活
        """
        alive, usage_info, error = await fetch_mantle_usage(acc)
        if alive and usage_info:
            acc.healthy = True
            acc.fail_count = 0
            acc.message_limit = usage_info["total"]
            acc.message_usage = usage_info["used"]
            acc.usage_count = usage_info["used"]
            acc.plan_type = usage_info.get("planType", "free")
            acc.period_started_at = usage_info.get("periodStartedAt")
            acc.resets_at = usage_info.get("resetsAt")

            if acc.message_usage < acc.message_limit:
                acc.mark_quota_recovered()
            else:
                acc.mark_quota_exhausted()
            log.info(f"[{acc.email}] 保活探测成功: 使用量 {acc.message_usage}/{acc.message_limit} (剩余 {acc.remaining})")
        else:
            acc.healthy = False
            log.warning(f"[{acc.email}] 保活探测失败: {error}")

        if save:
            self.save()
        return alive

    async def report_success(self, account: AccountState):
        """
        对话成功后本地计数加 1，若达到上限打标配额耗尽
        """
        account.usage_count += 1
        account.message_usage += 1
        account.fail_count = 0
        account.healthy = True
        if account.message_usage >= account.message_limit:
            account.mark_quota_exhausted()
            log.warning(f"账号 {account.email} 月度配额已耗尽 ({account.message_usage}/{account.message_limit})")
        self.save()

    async def report_failure(self, account: AccountState, reason: str = ""):
        account.fail_count += 1
        log.warning(f"账号 {account.email} 失败计次: {account.fail_count}/{FAIL_THRESHOLD}, 原因: {reason}")
        if account.fail_count >= FAIL_THRESHOLD:
            account.healthy = False
            log.error(f"账号 {account.email} 连续失败超限，已标记不健康")
        self.save()

    def get_tokens_detail(self) -> dict:
        tokens_list = []
        total_remaining = 0
        alive_count = 0

        with self._sync_lock:
            for acc in self.accounts:
                rem = acc.remaining
                total_remaining += rem

                if acc.healthy and not acc.disabled and rem > 0:
                    alive_count += 1

                tokens_list.append({
                    "index": acc.index,
                    "email": acc.email,
                    "token_hint": acc.token_hint,
                    "is_alive": acc.healthy,
                    "disabled": acc.disabled,
                    "remaining": rem,
                    "usage_count": acc.usage_count,
                    "message_usage": acc.message_usage,
                    "message_limit": acc.message_limit,
                    "fail_count": acc.fail_count,
                    "plan_type": getattr(acc, "plan_type", "free"),
                    "resets_at": getattr(acc, "resets_at", None),
                })

        return {
            "tokens": tokens_list,
            "summary": {
                "total": len(self.accounts),
                "alive": alive_count,
                "total_remaining": total_remaining,
                "last_refresh": self.last_refresh,
            },
        }


cm = CookieManager()