"""Dispatcher and Trigger types.

github_connect constructs Trigger objects from webhook payloads and calls
Dispatcher.dispatch(). All routing and pipeline logic lives here, not in
github_connect.
"""

from __future__ import annotations

from dataclasses import dataclass

from .context import ApplicationContext, SessionContext, session
from .pipelines import build_comment_workflow, build_pr_workflow
from .ports import CodeReader, Commenter


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PROpenedTrigger:
    installation_id: int
    repo:            str   # "owner/repo"
    pr_number:       int
    commit_sha:      str
    actor:           str
    reader:          CodeReader
    commenter:       Commenter


@dataclass(frozen=True)
class PRSyncTrigger:
    installation_id: int
    repo:            str
    pr_number:       int
    commit_sha:      str
    actor:           str
    reader:          CodeReader
    commenter:       Commenter


@dataclass(frozen=True)
class CommentReceivedTrigger:
    installation_id: int
    repo:            str
    pr_number:       int
    commit_sha:      str
    actor:           str
    comment_id:      str
    comment_body:    str
    reader:          CodeReader
    commenter:       Commenter


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class Dispatcher:
    def __init__(self, app_ctx: ApplicationContext) -> None:
        self._app_ctx = app_ctx

    async def dispatch(
        self,
        trigger: PROpenedTrigger | PRSyncTrigger | CommentReceivedTrigger,
    ) -> None:
        async with self._app_ctx.pr_lock(trigger.installation_id, trigger.pr_number):
            metadata = await trigger.reader.fetch_pr_metadata(trigger.pr_number)

            if metadata.draft and not self._app_ctx.config.review_drafts:
                await trigger.commenter.post_comment(
                    trigger.pr_number,
                    "Draft PR detected — Argus will review when marked ready.",
                )
                return

            ctx = SessionContext(
                installation_id=trigger.installation_id,
                repo=trigger.repo,
                pr_number=trigger.pr_number,
                commit_sha=trigger.commit_sha,
                actor=trigger.actor,
                reader=trigger.reader,
                commenter=trigger.commenter,
                metadata=metadata,
            )
            async with session(ctx):
                workflow = self._build_workflow(trigger)
                await workflow.run(None)

    def _build_workflow(
        self,
        trigger: PROpenedTrigger | PRSyncTrigger | CommentReceivedTrigger,
    ):
        match trigger:
            case PROpenedTrigger() | PRSyncTrigger():
                return build_pr_workflow(self._app_ctx)
            case CommentReceivedTrigger():
                return build_comment_workflow(self._app_ctx, trigger.comment_body)
