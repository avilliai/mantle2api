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
                verify=False
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
                verify=False
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("results"):
                    return data["results"][0].get("raw")
            return None
        except Exception as e:
            print(f"获取邮件失败: {e}")
            return None

    def get_address_id_by_name(self, name: str) -> int | None:
        """通过邮箱名搜索，拿到 address id"""
        try:
            res = requests.get(
                f"https://{self.worker_domain}/admin/address",
                params={"query": name, "limit": 10, "offset": 0},
                headers={"x-admin-auth": self.admin_password},
                timeout=10,
                verify=False
            )
            if res.status_code == 200:
                data = res.json()
                results = data.get("results", [])
                # 精确匹配，避免前缀相似的误命中
                full_address = f"{name}@{self.email_domain}"
                for item in results:
                    if item.get("name") == full_address or item.get("name") == name:
                        return item.get("id")
                print(f"[-] 未找到精确匹配: {name}，返回结果: {[r.get('name') for r in results]}")
            else:
                print(f"[-] 查询失败: {res.status_code} - {res.text}")
        except Exception as e:
            print(f"[-] 查询异常: {e}")
        return None

    def get_jwt_by_address_id(self, address_id: int) -> str | None:
        """用 address id 直接生成 JWT，无需删除重建"""
        try:
            res = requests.get(
                f"https://{self.worker_domain}/admin/show_password/{address_id}",
                headers={"x-admin-auth": self.admin_password},
                timeout=10,
                verify=False
            )
            if res.status_code == 200:
                return res.json().get("jwt")
            print(f"[-] 获取 JWT 失败: {res.status_code} - {res.text}")
        except Exception as e:
            print(f"[-] 获取 JWT 异常: {e}")
        return None

    def recover_email_jwt(self, name: str) -> tuple[str | None, str | None]:
        """
        通过邮箱名恢复 JWT（用于找回之前未保存的 JWT）
        :return: (jwt, address)
        """
        address_id = self.get_address_id_by_name(name)
        if not address_id:
            return None, None

        jwt = self.get_jwt_by_address_id(address_id)
        if jwt:
            address = f"{name}@{self.email_domain}"
            print(f"[+] 成功恢复 JWT: {address}")
            return jwt, address
        return None, None