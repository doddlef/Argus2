from __future__ import annotations

import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request # type: ignore

from agent_core.dispatcher import Dispatcher

from .adapters import GitHubCodeReader, GitHubCommenter
from .auth import GitHubAppAuth, GitHubAuthConfig
from .github_api import GitHubApiClient
from .state_store import StateStore
from .translator import build_trigger, translate_event

logger = logging.getLogger(__name__)


def create_app(
    dispatcher: Dispatcher,
    *,
    bot_username: str,
    state_store: StateStore | None = None,
    api_client: GitHubApiClient | None = None,
    webhook_secret: str | None = None,
    enable_debug_dispatch: bool = False,
) -> FastAPI:
    if state_store is None:
        db_path = Path(os.environ["ARGUS_STATE_DB_PATH"])
        state_store = StateStore(db_path)
    if webhook_secret is None:
        webhook_secret = os.environ["GITHUB_WEBHOOK_SECRET"]
    if api_client is None:
        auth_cfg = GitHubAuthConfig.from_env()
        api_client = GitHubApiClient(GitHubAppAuth(auth_cfg))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await api_client.close()

    app = FastAPI(lifespan=lifespan)

    async def _process_delivery(delivery_id: str, event: str, payload: dict) -> None:
        installation_id = payload.get("installation", {}).get("id")
        repo = payload.get("repository", {}).get("full_name")
        translation = translate_event(event=event, payload=payload, bot_username=bot_username)
        if translation.kind == "ignored":
            state_store.mark_ignored(delivery_id)
            return

        reader = GitHubCodeReader(api_client, int(installation_id), str(repo))
        commenter = GitHubCommenter(api_client, int(installation_id), str(repo))
        trigger = build_trigger(
            translation=translation,
            payload=payload,
            reader=reader,
            commenter=commenter,
        )
        if trigger is None:
            state_store.mark_ignored(delivery_id)
            return

        try:
            await dispatcher.dispatch(trigger)
            state_store.mark_processed(delivery_id)
        except Exception as exc:
            logger.exception("Delivery %s failed: %s", delivery_id, exc)
            state_store.mark_failed(delivery_id, str(exc))

    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
    ) -> dict:
        body = await request.body()
        if not _verify_signature(body, x_hub_signature_256, webhook_secret):
            raise HTTPException(status_code=401, detail="invalid signature")
        if not x_github_delivery or not x_github_event:
            raise HTTPException(status_code=400, detail="missing github headers")
        payload = await request.json()
        claim = state_store.claim_or_skip(
            delivery_id=x_github_delivery,
            event=x_github_event,
            installation_id=payload.get("installation", {}).get("id"),
            repo=payload.get("repository", {}).get("full_name"),
        )
        if claim.action == "skip_processed":
            return {"status": "duplicate_skipped"}
        try:
            background_tasks.add_task(_process_delivery, x_github_delivery, x_github_event, payload)
        except Exception as exc:
            state_store.mark_failed(x_github_delivery, f"enqueue failed: {exc}")
            raise HTTPException(status_code=500, detail="enqueue failed")
        return {"status": "accepted"}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": state_store.ping()}

    if enable_debug_dispatch:
        @app.post("/debug/dispatch")
        async def debug_dispatch(payload: dict, event: str) -> dict:
            await _process_delivery("debug-delivery", event, payload)
            return {"status": "ok"}

    return app


def _verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature)
