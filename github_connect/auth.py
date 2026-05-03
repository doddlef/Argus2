from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import jwt


@dataclass(frozen=True)
class GitHubAuthConfig:
    app_id: str
    private_key_pem: str
    webhook_secret: str

    @classmethod
    def from_env(cls) -> "GitHubAuthConfig":
        app_id = os.environ["GITHUB_APP_ID"]
        webhook_secret = os.environ["GITHUB_WEBHOOK_SECRET"]
        private_key_pem = os.environ.get("GITHUB_APP_PRIVATE_KEY_PEM")
        if not private_key_pem:
            key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
            if not key_path:
                raise KeyError("Set GITHUB_APP_PRIVATE_KEY_PEM or GITHUB_APP_PRIVATE_KEY_PATH")
            private_key_pem = Path(key_path).read_text(encoding="utf-8")
        return cls(app_id=app_id, private_key_pem=private_key_pem, webhook_secret=webhook_secret)


class GitHubAppAuth:
    def __init__(self, cfg: GitHubAuthConfig) -> None:
        self._cfg = cfg

    def make_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self._cfg.app_id}
        return jwt.encode(payload, self._cfg.private_key_pem, algorithm="RS256")

