# app/cookie_manager.py

import asyncio
import datetime
import json
import logging
import os
import threading
from typing import Optional, List

import dotenv
from fastapi import HTTPException

from app.config import TOKENS_FILE, ACTIVE_POOL_FILE, ACTIVE_POOL_SIZE
from app.models import AccountState, ACCOUNT_QUOTA
from app.usage_client import fetch_mantle_usage

log = logging.getLogger("mantle_proxy.cookie_manager")

dotenv.load_dotenv()

FAIL_THRESHOLD = int(os.environ.get("ACCOUNT_FAIL_THRESHOLD", "3"))


class CookieManager:
    def __init__(self):
        self.accounts: List[AccountState] = []
        self.active_pool: List[int] = []  # ? account.index
        self._idx: int = 0
        self._idx_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()
        self._replenish_lock = asyncio.Lock()
        self._replenishing: bool = False
        self.last_refresh: Optional[str] = None

    def load(self) -> None:
        if not TOKENS_FILE.exists():
            log.warning(f"??? {TOKENS_FILE}????????")
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
            log.info(f"???? Mantle ??: {len(self.accounts)} ?")

            # ?? active_pool
            if ACTIVE_POOL_FILE.exists():
                try:
                    pool_data = json.loads(ACTIVE_POOL_FILE.read_text(encoding="utf-8"))
                    if isinstance(pool_data, list):
                        self.active_pool = [int(x) for x in pool_data]
                except Exception as pe:
                    log.warning(f"?? {ACTIVE_POOL_FILE} ???????????: {pe}")
                    self.active_pool = []
            else:
                self.active_pool = []
        except Exception as e:
            log.error(f"?? {TOKENS_FILE} ??: {e}", exc_info=True)

    def save(self) -> None:
        with self._sync_lock:
            data = [a.to_dict() for a in self.accounts]
            TOKENS_FILE.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            ACTIVE_POOL_FILE.write_text(
                json.dumps(self.active_pool, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    async def async_save(self) -> None:
        await asyncio.to_thread(self.save)

    def _find_by_index(self, idx: int) -> Optional[AccountState]:
        return next((a for a in self.accounts if a.index == idx), None)

    @staticmethod
    def _account_is_eligible(acc: Optional[AccountState]) -> bool:
        """
        ???????????????????????
        """
        if not acc:
            return False
        return acc.healthy and not acc.disabled and not acc.quota_exhausted

    async def _check_and_recover_if_due(self, acc: AccountState) -> bool:
        """
        ???????????????????????/??? resets_at?
        ??? usage-status ?????????????????????
        """
        if acc.quota_exhausted_flag or acc.disabled:
            if acc.is_reset_due():
                log.info(f"? ?? [{acc.email}] ??????????? ({acc.resets_at})???????...")
                alive, usage_info, _ = await fetch_mantle_usage(acc)
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
                        log.info(f"?? ?? [{acc.email}] ???????????: ?? {acc.remaining}/{acc.message_limit}")
                        self.save()
                        return True
                    else:
                        acc.mark_quota_exhausted(usage_info.get("resetsAt"))
                        log.warning(f"?? ?? [{acc.email}] ??????????: {acc.message_usage}/{acc.message_limit}")
                        self.save()
                        return False
        return False

    async def _replenish_active_pool(self) -> None:
        """
        ?????? ACTIVE_POOL_SIZE?
        1. ??????????? (usage_count == 0 ? ???????)
        2. ??????????
        3. ???????????????????????????
        4. ?????????????? usage ?????????????
        5. ????????????????????????
        """
        if self._replenishing:
            return
        async with self._replenish_lock:
            self._replenishing = True
            try:
                # ?????????????????????????
                with self._sync_lock:
                    valid_pool = []
                    for idx in self.active_pool:
                        acc = self._find_by_index(idx)
                        if acc and self._account_is_eligible(acc):
                            valid_pool.append(idx)
                    self.active_pool = valid_pool
                    need = ACTIVE_POOL_SIZE - len(self.active_pool)

                if need <= 0:
                    return

                # ?????????????????????????????
                for acc in self.accounts:
                    if acc.index not in self.active_pool and (acc.quota_exhausted_flag or acc.disabled):
                        await self._check_and_recover_if_due(acc)

                # ?????????
                # ???usage_count == 0 (??????)
                # ???usage_count > 0 ?????
                pool_set = set(self.active_pool)
                unused_candidates = [
                    a for a in self.accounts
                    if a.index not in pool_set and self._account_is_eligible(a) and (a.usage_count == 0 or a.message_usage == 0)
                ]
                used_candidates = [
                    a for a in self.accounts
                    if a.index not in pool_set and self._account_is_eligible(a) and not (a.usage_count == 0 or a.message_usage == 0)
                ]
                candidates = unused_candidates + used_candidates

                activated: List[AccountState] = []
                for acc in candidates:
                    if len(activated) >= need:
                        break
                    log.info(f"?? ???????????????????: {acc.email}")
                    alive, usage_info, err = await fetch_mantle_usage(acc)
                    if alive and usage_info:
                        acc.healthy = True
                        acc.fail_count = 0
                        acc.message_limit = usage_info["total"]
                        acc.message_usage = usage_info["used"]
                        acc.usage_count = usage_info["used"]
                        acc.plan_type = usage_info.get("planType", "free")
                        acc.period_started_at = usage_info.get("periodStartedAt")
                        acc.resets_at = usage_info.get("resetsAt")
                        if acc.message_usage >= acc.message_limit:
                            acc.mark_quota_exhausted(usage_info.get("resetsAt"))
                            log.warning(f"?? ?? [{acc.email}] ???? ({acc.message_usage}/{acc.message_limit})????????????? {acc.resets_at}")
                            continue
                        else:
                            acc.mark_quota_recovered()
                            activated.append(acc)
                            log.info(f"? ?? [{acc.email}] ??????????")
                    else:
                        acc.healthy = False
                        log.warning(f"? ?? [{acc.email}] ???? ({err})???")

                if activated:
                    with self._sync_lock:
                        for a in activated:
                            if a.index not in self.active_pool:
                                self.active_pool.append(a.index)
                    await self.async_save()
                    log.info(f"?? ???????? {len(activated)} ???: {[a.email for a in activated]}")

                # ??????????????????????????
                remaining_need = ACTIVE_POOL_SIZE - len(self.active_pool)
                if remaining_need > 0:
                    log.info(f"?? ????????? (?? {remaining_need} ?)??????????...")
                    for _ in range(remaining_need):
                        new_acc = await self._register_new_account()
                        if new_acc:
                            with self._sync_lock:
                                if new_acc.index not in self.active_pool:
                                    self.active_pool.append(new_acc.index)
                            await self.async_save()
                            log.info(f"?? ?????????????????: {new_acc.email}")
                        else:
                            log.error("?????????")
                            break
            finally:
                self._replenishing = False

    async def _register_new_account(self) -> Optional[AccountState]:
        """????? curl_cffi ????????"""
        try:
            from app.mantle_client import MantleRegister
            reg = MantleRegister()
            res = await reg.run()
            if res:
                acc = self.add_account(res)
                # ??????? usage
                await self.check_session(acc)
                return acc
        except Exception as e:
            log.error(f"?????????: {e}", exc_info=True)
        return None

    async def get_account(self) -> AccountState:
        """
        ??????????????
        - ?? active_pool ???????
        - ????????????????? _replenish_active_pool?
        - ??????????????????????????????
        """
        # 1. ????????
        with self._sync_lock:
            pool_set = set(self.active_pool)
            valid = [
                a for a in self.accounts
                if a.index in pool_set and self._account_is_eligible(a)
            ]

        if not valid or len(valid) < ACTIVE_POOL_SIZE:
            await self._replenish_active_pool()
            with self._sync_lock:
                pool_set = set(self.active_pool)
                valid = [
                    a for a in self.accounts
                    if a.index in pool_set and self._account_is_eligible(a)
                ]

        if not valid:
            raise HTTPException(
                status_code=503,
                detail="????? Mantle ???????/??????",
            )

        async with self._idx_lock:
            acc = valid[self._idx % len(valid)]
            self._idx = (self._idx + 1) % len(valid)
            return acc

    def add_account(self, data: dict) -> AccountState:
        with self._sync_lock:
            idx = len(self.accounts) + 1
            acc = AccountState(data, idx)
            self.accounts.append(acc)
        self.save()
        log.info(f"? Mantle ???????? (index={idx}, email={acc.email})")
        return acc

    async def check_session(self, acc: AccountState, save: bool = True) -> bool:
        """
        ?? mantle usage-status ???????????
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
                acc.mark_quota_exhausted(usage_info.get("resetsAt"))
                # ???????
                if acc.index in self.active_pool:
                    self.active_pool.remove(acc.index)
            log.info(f"[{acc.email}] ????: ??? {acc.message_usage}/{acc.message_limit} (?? {acc.remaining})")
        else:
            acc.healthy = False
            log.warning(f"[{acc.email}] ????: {error}")

        if save:
            self.save()
        return alive

    async def handle_quota_exhausted(self, account: AccountState):
        """
        ??????????????????
        ?????????????????????????????????????
        ??????????
        """
        log.warning(f"?? ?? [{account.email}] ??????????????????...")
        alive, usage_info, _ = await fetch_mantle_usage(account)
        resets_at = None
        if alive and usage_info:
            account.message_limit = usage_info.get("total", account.message_limit)
            account.message_usage = usage_info.get("used", account.message_limit)
            account.usage_count = account.message_usage
            resets_at = usage_info.get("resetsAt")
        account.mark_quota_exhausted(resets_at)

        with self._sync_lock:
            if account.index in self.active_pool:
                self.active_pool.remove(account.index)
        self.save()
        log.info(f"?? [{account.email}] ???????????????: {account.resets_at}")

        # ???????
        asyncio.create_task(self._replenish_active_pool())

    async def report_success(self, account: AccountState):
        """
        ?????????? 1???????????????
        """
        account.usage_count += 1
        account.message_usage += 1
        account.fail_count = 0
        account.healthy = True
        if account.message_usage >= account.message_limit:
            await self.handle_quota_exhausted(account)
        else:
            self.save()

    async def _relogin_account(self, account: AccountState) -> bool:
        """
        ?????401?Token???????????????????Session?
        ?????????????????? 2G ???????
        """
        try:
            from app.mantle_client import relogin_with_browser
            log.info(f"?? ?????????????: {account.email}")
            result = await relogin_with_browser(account.email)
            if result and result.get("cookies"):
                account.cookies = result["cookies"]
                if result.get("csrf_token"):
                    account.csrf_token = result["csrf_token"]
                account.fail_count = 0
                account.healthy = True
                self.save()
                log.info(f"? ??????????: {account.email}")
                # ???? usage ??
                await self.check_session(account)
                return True
            else:
                log.warning(f"? ????????: {account.email}")
                return False
        except Exception as e:
            log.error(f"? ????????: {account.email} - {e}", exc_info=True)
            return False

    async def report_failure(self, account: AccountState, reason: str = ""):
        # 1. ??????
        if "quota" in reason.lower() or "limit" in reason.lower() or "exhausted" in reason.lower():
            await self.handle_quota_exhausted(account)
            return

        # 2. ???? / 401 / Token???????????
        is_auth_error = any(k in reason.lower() for k in ["401", "authentication required", "unauthorized", "token"])
        if is_auth_error:
            log.warning(f"?? ?? [{account.email}] ????/401???????????...")
            # ???????????
            async def _bg_relogin():
                success = await self._relogin_account(account)
                if not success:
                    account.fail_count += 1
                    if account.fail_count >= FAIL_THRESHOLD:
                        account.healthy = False
                        with self._sync_lock:
                            if account.index in self.active_pool:
                                self.active_pool.remove(account.index)
                        asyncio.create_task(self._replenish_active_pool())
                    self.save()
            asyncio.create_task(_bg_relogin())
            return

        # 3. ??????
        account.fail_count += 1
        log.warning(f"?? {account.email} ????: {account.fail_count}/{FAIL_THRESHOLD}, ??: {reason}")
        if account.fail_count >= FAIL_THRESHOLD:
            account.healthy = False
            log.error(f"? {account.email} ??????????????")
            with self._sync_lock:
                if account.index in self.active_pool:
                    self.active_pool.remove(account.index)
            asyncio.create_task(self._replenish_active_pool())
        self.save()

    def get_tokens_detail(self) -> dict:
        tokens_list = []
        total_remaining = 0
        alive_count = 0
        active_set = set(self.active_pool)

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
                    "is_active": acc.index in active_set,
                    "quota_exhausted": acc.quota_exhausted,
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
                "active_pool_size": len(self.active_pool),
                "active_pool_target": ACTIVE_POOL_SIZE,
                "total_remaining": total_remaining,
                "last_refresh": self.last_refresh,
            },
        }


cm = CookieManager()
