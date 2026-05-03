from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent_core.dispatcher import CommentReceivedTrigger, PROpenedTrigger, PRSyncTrigger
from agent_core.ports import CodeReader, Commenter

TriggerKind = Literal["pr_opened", "pr_sync", "comment", "ignored"]


@dataclass(frozen=True)
class Translation:
    kind: TriggerKind
    reason: str = ""


def translate_event(
    *,
    event: str,
    payload: dict,
    bot_username: str,
) -> Translation:
    action = payload.get("action")
    if event == "pull_request" and action in {"opened", "reopened"}:
        return Translation(kind="pr_opened")
    if event == "pull_request" and action == "synchronize":
        return Translation(kind="pr_sync")
    if event == "issue_comment" and action == "created":
        issue = payload.get("issue", {})
        if not issue.get("pull_request"):
            return Translation(kind="ignored", reason="issue_comment_not_on_pr")
        if _author_login(payload.get("comment", {})) == bot_username:
            return Translation(kind="ignored", reason="bot_comment")
        return Translation(kind="comment")
    if event == "pull_request_review_comment" and action == "created":
        if _author_login(payload.get("comment", {})) == bot_username:
            return Translation(kind="ignored", reason="bot_review_comment")
        return Translation(kind="comment")
    return Translation(kind="ignored", reason="unsupported_event_action")


def build_trigger(
    *,
    translation: Translation,
    payload: dict,
    reader: CodeReader,
    commenter: Commenter,
) -> PROpenedTrigger | PRSyncTrigger | CommentReceivedTrigger | None:
    if translation.kind == "ignored":
        return None

    installation_id = int(payload["installation"]["id"])
    repo = payload["repository"]["full_name"]
    pr = payload["pull_request"] if "pull_request" in payload else payload["issue"]["pull_request"]

    if translation.kind in {"pr_opened", "pr_sync"}:
        pull = payload["pull_request"]
        common = dict(
            installation_id=installation_id,
            repo=repo,
            pr_number=int(pull["number"]),
            commit_sha=pull["head"]["sha"],
            actor=payload["sender"]["login"],
            reader=reader,
            commenter=commenter,
        )
        if translation.kind == "pr_opened":
            return PROpenedTrigger(**common)
        return PRSyncTrigger(
            **common,
            before_sha=str(payload.get("before") or "") or None,
        )

    # comment
    if "pull_request" in payload:
        pull = payload["pull_request"]
        pr_number = int(pull["number"])
        commit_sha = pull["head"]["sha"]
        comment = payload["comment"]
    else:
        issue = payload["issue"]
        pr_number = int(issue["number"])
        commit_sha = payload.get("repository", {}).get("default_branch", "HEAD")
        comment = payload["comment"]
    return CommentReceivedTrigger(
        installation_id=installation_id,
        repo=repo,
        pr_number=pr_number,
        commit_sha=commit_sha,
        actor=payload["sender"]["login"],
        comment_id=str(comment["id"]),
        comment_body=comment.get("body", ""),
        reader=reader,
        commenter=commenter,
    )


def _author_login(comment: dict) -> str:
    return comment.get("user", {}).get("login", "")
