from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import GitHubAppAuth


@dataclass
class _Token:
    token: str
    expires_at: float


class GitHubApiClient:
    def __init__(self, auth: GitHubAppAuth, base_url: str = "https://api.github.com") -> None:
        self._auth = auth
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0),
            headers={"Accept": "application/vnd.github+json"},
        )
        self._tokens: dict[int, _Token] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def get_json(self, installation_id: int, path: str, params: dict | None = None) -> Any:
        return await self._request_json("GET", installation_id, path, params=params)

    async def post_json(self, installation_id: int, path: str, json: dict | None = None) -> Any:
        return await self._request_json("POST", installation_id, path, json=json)

    async def _request_json(
        self,
        method: str,
        installation_id: int,
        path: str,
        params: dict | None = None,
        json: dict | None = None,
    ) -> Any:
        token = await self._installation_token(installation_id)
        headers = {"Authorization": f"Bearer {token}"}
        retries = 3
        for attempt in range(retries + 1):
            resp = await self._client.request(
                method=method,
                url=path,
                params=params,
                json=json,
                headers=headers,
            )
            if resp.status_code < 400:
                if resp.text:
                    return resp.json()
                return {}
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    await asyncio.sleep(int(retry_after))
                else:
                    await asyncio.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"GitHub API error {resp.status_code}: {resp.text[:400]}")
        raise RuntimeError("unreachable")

    async def _installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        now = time.time()
        if cached and now < cached.expires_at - 300:
            return cached.token

        app_jwt = self._auth.make_jwt()
        resp = await self._client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Token mint failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        token = data["token"]
        expires_at = time.time() + 3600
        self._tokens[installation_id] = _Token(token=token, expires_at=expires_at)
        return token

