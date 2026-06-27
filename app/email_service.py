"""
邮箱服务类（与原项目相同）
"""
import os
import requests
import random
import string
from dotenv import load_dotenv


class EmailService:
    def __init__(self):
        load_dotenv()
        self.worker_domain  = os.getenv("WORKER_DOMAIN")
        self.email_domain   = os.getenv("EMAIL_DOMAIN")
        self.admin_password = os.getenv("ADMIN_PASSWORD")

        if not all([self.worker_domain, self.email_domain, self.admin_password]):
            raise ValueError(
                "Missing required environment variables: WORKER_DOMAIN, EMAIL_DOMAIN, ADMIN_PASSWORD"
            )

    def _generate_random_name(self) -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 6)))
        numbers  = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(0, 5)))
        return letters1 + numbers + letters2

    def create_email(self):
        url = f"https://{self.worker_domain}/admin/new_address"
        try:
            name = self._generate_random_name()
            res  = requests.post(
                url,
                json={
                    "enablePrefix": True,
                    "name":         name,
                    "domain":       self.email_domain,
                },
                headers={
                    "x-admin-auth":  self.admin_password,
                    "Content-Type":  "application/json",
                },
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("jwt"), data.get("address")
            print(f"[-] 创建邮箱接口返回错误: {res.status_code} - {res.text}")
            return None, None
        except Exception as e:
            print(f"[-] 创建邮箱网络异常: {e}")
            return None, None

    def fetch_first_email(self, jwt: str):
        try:
            res = requests.get(
                f"https://{self.worker_domain}/api/mails",
                params={"limit": 10, "offset": 0},
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type":  "application/json",
                },
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("results"):
                    return data["results"][0].get("raw")
            return None
        except Exception as e:
            print(f"获取邮件失败: {e}")
            return None
