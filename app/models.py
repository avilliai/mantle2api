# app/models.py

ACCOUNT_QUOTA = 100  # 每个账号永久限额 100 次，上游不会重置

class AccountState:
    def __init__(self, d: dict, index: int = 1):
        self.index = index
        self.email = d.get("email", "")
        self.name = d.get("name", "")
        self.cookie_str = d.get("cookie_str", "")
        self.user_id = d.get("user_id", "")
        self.disabled = d.get("disabled", False)
        self.healthy = d.get("healthy", True)
        self.usage_count = d.get("usage_count", 0)

    @property
    def remaining(self) -> int:
        """剩余额度"""
        return max(0, ACCOUNT_QUOTA - self.usage_count)

    @property
    def is_alive(self) -> bool:
        """账号是否存活（健康、未禁用、额度未耗尽）"""
        return self.healthy and not self.disabled and self.remaining > 0

    @property
    def quota_exhausted(self) -> bool:
        """额度是否耗尽"""
        return self.usage_count >= ACCOUNT_QUOTA

    @property
    def token_hint(self) -> str:
        """提取并脱敏 session-token，供前端 Token 列展示"""
        if not self.cookie_str:
            return "N/A"
        for part in self.cookie_str.split(";"):
            part = part.strip()
            if part.startswith("__Secure-authjs.session-token="):
                val = part.split("=", 1)[1]
                if len(val) > 20:
                    return f"{val[:10]}...{val[-10:]}"
                return val
        return "N/A"

    def cookies_dict(self) -> dict:
        """将 cookie 字符串转换为 dict 格式"""
        if not self.cookie_str:
            return {}
        res = {}
        for part in self.cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                res[k.strip()] = v.strip()
        return res

    def increment_usage(self) -> None:
        """递增用量；达到上限后由 CookieManager 永久删除账号"""
        self.usage_count += 1
        if self.quota_exhausted:
            self.disabled = True

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "name": self.name,
            "cookie_str": self.cookie_str,
            "user_id": self.user_id,
            "disabled": self.disabled,
            "healthy": self.healthy,
            "usage_count": self.usage_count,
        }
