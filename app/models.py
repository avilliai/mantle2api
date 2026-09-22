# app/models.py

from datetime import datetime, timezone

ACCOUNT_QUOTA = 1500  # Mantle ?????? 1500??? usage-status ????


class AccountState:
    def __init__(self, d: dict, index: int = 1):
        self.index = index
        self.email = d.get("email", "")
        self.user_id = d.get("user_id", "")
        self.auth_uid = d.get("auth_uid", "")
        self.workspace_id = d.get("workspace_id", "")
        self.csrf_token = d.get("csrf_token", "")
        self.cookies = d.get("cookies", {})
        self.cookie_str = d.get("cookie_str", "")
        self.disabled = d.get("disabled", False)
        self.healthy = d.get("healthy", True)
        self.usage_count = d.get("usage_count", 0)
        self.message_limit = d.get("message_limit", ACCOUNT_QUOTA)
        self.message_usage = d.get("message_usage", self.usage_count)
        self.quota_exhausted_flag = d.get("quota_exhausted", False)
        self.fail_count = d.get("fail_count", 0)
        self.plan_type = d.get("plan_type", "free")
        self.period_started_at = d.get("period_started_at", None)
        self.resets_at = d.get("resets_at", None)

        if not self.cookies and self.cookie_str:
            self.cookies = self.cookies_dict()
        elif not self.cookie_str and self.cookies:
            self.cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    @property
    def remaining(self) -> int:
        """????"""
        limit = self.message_limit if self.message_limit > 0 else ACCOUNT_QUOTA
        return max(0, limit - self.message_usage)

    @property
    def is_alive(self) -> bool:
        """???????????????????????"""
        return self.healthy and not self.disabled and not self.quota_exhausted

    @property
    def quota_exhausted(self) -> bool:
        """???????"""
        if self.quota_exhausted_flag:
            return True
        limit = self.message_limit if self.message_limit > 0 else ACCOUNT_QUOTA
        return self.message_usage >= limit

    def is_reset_due(self) -> bool:
        """?????????????????????????"""
        if not self.resets_at:
            return False
        try:
            clean_str = self.resets_at.replace("Z", "+00:00")
            reset_time = datetime.fromisoformat(clean_str)
            now = datetime.now(timezone.utc)
            return now >= reset_time
        except Exception:
            return False

    def mark_quota_exhausted(self, resets_at: str = None) -> None:
        """????????????"""
        self.quota_exhausted_flag = True
        self.disabled = True
        if resets_at:
            self.resets_at = resets_at

    def mark_quota_recovered(self) -> None:
        """???????????"""
        self.quota_exhausted_flag = False
        self.disabled = False
        self.healthy = True
        self.fail_count = 0

    @property
    def token_hint(self) -> str:
        """? cookie ??? wos-session ?? Token ??"""
        session = self.cookies.get("wos-session") or ""
        if session and len(session) > 20:
            return f"{session[:10]}...{session[-10:]}"
        return self.email or "N/A"

    def cookies_dict(self) -> dict:
        """? cookie ?????? dict ??"""
        if self.cookies:
            return dict(self.cookies)
        if not self.cookie_str:
            return {}
        res = {}
        for part in self.cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                res[k.strip()] = v.strip()
        return res

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "user_id": self.user_id,
            "auth_uid": self.auth_uid,
            "workspace_id": self.workspace_id,
            "csrf_token": self.csrf_token,
            "cookies": self.cookies,
            "cookie_str": self.cookie_str,
            "disabled": self.disabled,
            "healthy": self.healthy,
            "usage_count": self.usage_count,
            "message_limit": self.message_limit,
            "message_usage": self.message_usage,
            "quota_exhausted": self.quota_exhausted_flag,
            "fail_count": self.fail_count,
            "plan_type": self.plan_type,
            "period_started_at": self.period_started_at,
            "resets_at": self.resets_at,
        }
