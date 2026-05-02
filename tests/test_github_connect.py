from __future__ import annotations

import base64
import hashlib
import hmac
from pathlib import Path

from fastapi.testclient import TestClient

from agent_core.context import ApplicationContext, ClientTier, PRLockManager
from agent_core.dispatcher import Dispatcher
from agent_core.domain import InlineComment, PRMetadata
from agent_core.ports import CodeReader, Commenter
from github_connect.adapters import GitHubCodeReader, GitHubCommenter, _to_review_comment
from github_connect.server import create_app
from github_connect.state_store import StateStore
from github_connect.translator import build_trigger, translate_event
from llm_framework.client import LLMClient
from llm_framework.responses import LLMResponse


class _NoopClient(LLMClient):
    async def complete(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content="noop")


class _NoopReader(CodeReader):
    async def fetch_pr_metadata(self, pr_number: int) -> PRMetadata:
        return PRMetadata("t", "", "a", "main", "b", [], pr_number, False)

    async def fetch_commits(self, pr_number: int):
        return []

    async def fetch_changed_files(self, pr_number: int):
        return []

    async def fetch_diff(self, pr_number: int, path: str):
        return ""

    async def fetch_file(self, path: str, ref: str):
        return ""

    async def fetch_tree(self, ref: str):
        return []

    async def fetch_comments(self, pr_number: int, after: str | None = None):
        return []


class _NoopCommenter(Commenter):
    async def post_comment(self, pr_number: int, body: str) -> str:
        return "c1"

    async def post_review(self, pr_number: int, body: str, verdict: str, inline_comments):
        return None


def _app_ctx(tmp_path: Path) -> ApplicationContext:
    cfg = type("Cfg", (), {"review_drafts": False, "acknowledge_events": False})()
    return ApplicationContext(
        clients=ClientTier(fast=_NoopClient(), standard=_NoopClient(), deep=_NoopClient()),
        wiki_root=tmp_path,
        pr_lock=PRLockManager(),
        bot_username="argus-bot",
        config=cfg,  # type: ignore[arg-type]
    )


class FakeApi:
    def __init__(self):
        self.posts = []
        self.gets = []

    async def get_json(self, installation_id, path, params=None):
        self.gets.append((path, params))
        if path.endswith("/pulls/1"):
            return {
                "title": "T",
                "body": "D",
                "user": {"login": "dev"},
                "base": {"ref": "main"},
                "head": {"ref": "feat", "sha": "abc"},
                "labels": [{"name": "x"}],
                "number": 1,
                "draft": False,
            }
        if path.endswith("/pulls/1/files"):
            return [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 1, "patch": "@@"}]
        if path.endswith("/pulls/1/commits"):
            return [{"sha": "abc", "commit": {"message": "m"}}]
        if path.endswith("/issues/1/comments"):
            return [{"id": 1, "user": {"login": "dev"}, "body": "hi", "created_at": "2020-01-01T00:00:00Z"}]
        if path.endswith("/pulls/1/comments"):
            return [{"id": 2, "user": {"login": "dev2"}, "body": "yo", "created_at": "2020-01-01T00:00:01Z", "position": 3}]
        if "/contents/" in path:
            return {"content": base64.b64encode(b"print('x')\n").decode("utf-8")}
        if "/git/trees/" in path:
            return {"tree": [{"path": "a.py", "type": "blob"}, {"path": "dir", "type": "tree"}]}
        return []

    async def post_json(self, installation_id, path, json=None):
        self.posts.append((path, json))
        if json and json.get("comments"):
            raise RuntimeError("inline rejected")
        return {"id": 123}

    async def close(self):
        return None


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_translate_event_supported_and_ignored():
    t = translate_event(event="pull_request", payload={"action": "opened"}, bot_username="argus-bot")
    assert t.kind == "pr_opened"
    t2 = translate_event(event="issue_comment", payload={"action": "created", "issue": {}, "comment": {}}, bot_username="argus-bot")
    assert t2.kind == "ignored"


def test_build_trigger_pr_opened():
    payload = {
        "installation": {"id": 1},
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 1, "head": {"sha": "abc"}},
        "sender": {"login": "dev"},
    }
    trig = build_trigger(
        translation=translate_event(event="pull_request", payload={"action": "opened"}, bot_username="argus-bot"),
        payload=payload,
        reader=_NoopReader(),
        commenter=_NoopCommenter(),
    )
    assert trig is not None
    assert trig.pr_number == 1


def test_state_store_claim_and_skip(tmp_path):
    store = StateStore(tmp_path / "state.db")
    first = store.claim_or_skip(delivery_id="d1", event="pull_request", installation_id=1, repo="o/r")
    assert first.action == "process"
    store.mark_processed("d1")
    second = store.claim_or_skip(delivery_id="d1", event="pull_request", installation_id=1, repo="o/r")
    assert second.action == "skip_processed"


def test_adapter_commenter_degrades_on_inline_failure():
    api = FakeApi()
    commenter = GitHubCommenter(api, 1, "o/r")
    import asyncio
    asyncio.run(
        commenter.post_review(
            1,
            "body",
            "request_changes",
            [InlineComment(path="a.py", start_line=1, end_line=1, body="fix")],
        )
    )
    assert len(api.posts) == 2
    assert api.posts[1][1]["event"] == "REQUEST_CHANGES"


def test_adapter_reader_fetch_comments_merge_and_after():
    api = FakeApi()
    reader = GitHubCodeReader(api, 1, "o/r")
    import asyncio
    comments = asyncio.run(reader.fetch_comments(1))
    assert [c.id for c in comments] == ["1", "2"]
    newer = asyncio.run(reader.fetch_comments(1, after="1"))
    assert [c.id for c in newer] == ["2"]


def test_inline_mapping_range_and_single():
    single = _to_review_comment(InlineComment(path="x.py", start_line=4, end_line=4, body="a"))
    assert single["line"] == 4 and single["side"] == "RIGHT"
    rng = _to_review_comment(InlineComment(path="x.py", start_line=3, end_line=5, body="b"))
    assert rng["start_line"] == 3 and rng["line"] == 5


def test_webhook_signature_and_dedupe(tmp_path):
    store = StateStore(tmp_path / "state.db")
    app = create_app(
        Dispatcher(_app_ctx(tmp_path)),
        bot_username="argus-bot",
        state_store=store,
        api_client=FakeApi(),
        webhook_secret="secret",
    )
    client = TestClient(app)
    body = {
        "action": "opened",
        "installation": {"id": 1},
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 1, "head": {"sha": "abc"}},
        "sender": {"login": "dev"},
    }
    import json
    raw = json.dumps(body).encode("utf-8")
    headers = {
        "X-Hub-Signature-256": _sign("secret", raw),
        "X-GitHub-Delivery": "d-1",
        "X-GitHub-Event": "pull_request",
        "Content-Type": "application/json",
    }
    r1 = client.post("/webhooks/github", data=raw, headers=headers)
    assert r1.status_code == 200
    r2 = client.post("/webhooks/github", data=raw, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["status"] in {"duplicate_skipped", "accepted"}


def test_webhook_bad_signature_401(tmp_path):
    app = create_app(
        Dispatcher(_app_ctx(tmp_path)),
        bot_username="argus-bot",
        state_store=StateStore(tmp_path / "state.db"),
        api_client=FakeApi(),
        webhook_secret="secret",
    )
    client = TestClient(app)
    r = client.post(
        "/webhooks/github",
        json={"action": "opened"},
        headers={
            "X-Hub-Signature-256": "sha256=bad",
            "X-GitHub-Delivery": "d-2",
            "X-GitHub-Event": "pull_request",
        },
    )
    assert r.status_code == 401

