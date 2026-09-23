"""
???????? Cloudflare Worker ???????
"""
import os
import requests
import random
import string
from typing import Tuple, Optional
from dotenv import load_dotenv


class EmailService:
    def __init__(self):
        load_dotenv()
        self.worker_domain  = os.getenv("WORKER_DOMAIN")
        self.email_domain   = os.getenv("EMAIL_DOMAIN", "xedjwor.info")
        self.admin_password = os.getenv("ADMIN_PASSWORD")
        self.proxy          = os.getenv("HTTP_PROXY") or os.getenv("PROXY")

        if not all([self.worker_domain, self.email_domain, self.admin_password]):
            raise ValueError(
                "Missing required environment variables: WORKER_DOMAIN, EMAIL_DOMAIN, ADMIN_PASSWORD"
            )

        self.session = requests.Session()
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

    def _generate_random_name(self) -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 6)))
        numbers  = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(0, 5)))
        return letters1 + numbers + letters2

    def create_email(self, domain: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        target_domain = domain or self.email_domain
        url = f"https://{self.worker_domain}/admin/new_address"
        try:
            name = self._generate_random_name()
            res = self.session.post(
                url,
                json={
                    "enablePrefix": True,
                    "name":         name,
                    "domain":       target_domain,
                },
                headers={
                    "x-admin-auth":  self.admin_password,
                    "Content-Type":  "application/json",
                },
                timeout=15,
                verify=False
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("jwt"), data.get("address")
            print(f"[-] ??????????: {res.status_code} - {res.text}")
            return None, None
        except Exception as e:
            print(f"[-] ????????: {e}")
            return None, None

    def fetch_first_email(self, jwt: str) -> Optional[str]:
        try:
            res = self.session.get(
                f"https://{self.worker_domain}/api/mails",
                params={"limit": 10, "offset": 0},
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type":  "application/json",
                },
                timeout=15,
                verify=False
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("results"):
                    return data["results"][0].get("raw")
            return None
        except Exception as e:
            print(f"??????: {e}")
            return None

    def get_address_id_by_name(self, name_or_email: str) -> Optional[int]:
        """????????????????? address id"""
        try:
            if "@" in name_or_email:
                prefix, domain = name_or_email.split("@", 1)
                full_address = name_or_email
            else:
                prefix = name_or_email
                domain = self.email_domain
                full_address = f"{prefix}@{domain}"

            res = self.session.get(
                f"https://{self.worker_domain}/admin/address",
                params={"query": prefix, "limit": 20, "offset": 0},
                headers={"x-admin-auth": self.admin_password},
                timeout=15,
                verify=False
            )
            if res.status_code == 200:
                data = res.json()
                results = data.get("results", [])
                # ???????????
                for item in results:
                    item_name = item.get("name", "")
                    if item_name == full_address or item_name == prefix:
                        return item.get("id")
                # ?????????????????? prefix@ ???
                for item in results:
                    item_name = item.get("name", "")
                    if item_name.startswith(f"{prefix}@"):
                        return item.get("id")
                print(f"[-] ???????: {name_or_email}?????: {[r.get('name') for r in results]}")
            else:
                print(f"[-] ????: {res.status_code} - {res.text}")
        except Exception as e:
            print(f"[-] ????: {e}")
        return None

    def get_jwt_by_address_id(self, address_id: int) -> Optional[str]:
        """? address id ???? JWT???????"""
        try:
            res = self.session.get(
                f"https://{self.worker_domain}/admin/show_password/{address_id}",
                headers={"x-admin-auth": self.admin_password},
                timeout=15,
                verify=False
            )
            if res.status_code == 200:
                return res.json().get("jwt")
            print(f"[-] ?? JWT ??: {res.status_code} - {res.text}")
        except Exception as e:
            print(f"[-] ?? JWT ??: {e}")
        return None

    def recover_email_jwt(self, name_or_email: str) -> Tuple[Optional[str], Optional[str]]:
        """
        ???????????? JWT??????????? JWT?
        :return: (jwt, full_address)
        """
        if "@" in name_or_email:
            full_address = name_or_email
        else:
            full_address = f"{name_or_email}@{self.email_domain}"

        address_id = self.get_address_id_by_name(name_or_email)
        if not address_id:
            return None, None

        jwt = self.get_jwt_by_address_id(address_id)
        if jwt:
            return jwt, full_address
        return None, None
