from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass


COOKIE_NAME = "kucoin_exposure_session"


@dataclass(frozen=True)
class Session:
    username: str
    expires_at: int
    csrf_token: str


class SessionSigner:
    def __init__(self, secret: str, session_days: int = 30):
        self._secret = secret.encode("utf-8")
        self.max_age = int(session_days * 86400)

    @staticmethod
    def _b64encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _b64decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def issue(self, username: str, now: int | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        payload = {
            "u": username,
            "exp": issued_at + self.max_age,
            "csrf": secrets.token_urlsafe(24),
        }
        encoded = self._b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = self._b64encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{encoded}.{signature}"

    def verify(self, token: str | None, now: int | None = None) -> Session | None:
        if not token or "." not in token:
            return None
        encoded, supplied_signature = token.rsplit(".", 1)
        expected_signature = self._b64encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        try:
            payload = json.loads(self._b64decode(encoded))
            expires_at = int(payload["exp"])
            username = str(payload["u"])
            csrf_token = str(payload["csrf"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        current = int(time.time() if now is None else now)
        if expires_at <= current:
            return None
        return Session(username=username, expires_at=expires_at, csrf_token=csrf_token)


def credentials_match(
    supplied_username: str,
    supplied_password: str,
    expected_username: str,
    expected_password: str,
) -> bool:
    username_ok = hmac.compare_digest(
        supplied_username.encode("utf-8"), expected_username.encode("utf-8")
    )
    password_ok = hmac.compare_digest(
        supplied_password.encode("utf-8"), expected_password.encode("utf-8")
    )
    return username_ok and password_ok
