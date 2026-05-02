"""Pipeline tests — wiring (structural) and behavior (end-to-end with mocks).

Wiring tests: inspect workflow.edges to assert correct graph structure without
running any nodes.

Behavior tests: run full pipelines via Dispatcher using scripted LLM clients
and stub ports. No real API calls are made.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Literal

import pytest

from agent_core.config import ArgusConfig, ClientConfig
from agent_core.context import ApplicationContext, ClientTier, PRLockManager, PipelinePayload, PipelineState
from agent_core.dispatcher import CommentReceivedTrigger, Dispatcher, PROpenedTrigger
from agent_core.domain import ChangedFile, Commit, Comment, InlineComment, PRMetadata
from agent_core.edges import PassThroughNode, PipelineStateFanOutEdge, PipelineStateJoinEdge
from agent_core.nodes.analysis import AnalysisNode, RouteNode, TriageNode
from agent_core.nodes.conversation import ConversationNode
from agent_core.nodes.loading import (
    AcknowledgeNode,
    MemLoadNode,
    PRLoadNode,
    ThreadLoadNode,
    TreeLoadNode,
)
from agent_core.nodes.output import SummaryNode, WikiMergeNode
from agent_core.pipelines import build_comment_workflow, build_pr_workflow
from agent_core.ports import CodeReader, Commenter
from llm_framework.client import LLMClient
from llm_framework.edges import ConditionalEdge, DirectedEdge
from llm_framework.responses import LLMResponse
from llm_framework.tools import ToolCall


# ---------------------------------------------------------------------------
# Scripted LLM client
# ---------------------------------------------------------------------------

class ScriptedClient(LLMClient):
    """Returns queued LLMResponse objects in order. Raises if exhausted."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._queue: deque[LLMResponse] = deque(responses)

    async def complete(self, messages, tools=None, system=None, options=None) -> LLMResponse:
        if not self._queue:
            raise RuntimeError("ScriptedClient exhausted — check test setup")
        return self._queue.popleft()


class _NoopClient(LLMClient):
    """Raises if called — used in wiring tests where no LLM calls should occur."""

    async def complete(self, *args, **kwargs) -> LLMResponse:
        raise RuntimeError("LLM client should not be called in wiring tests")


# ---------------------------------------------------------------------------
# Stub ports
# ---------------------------------------------------------------------------

class MockReader(CodeReader):
    def __init__(self, metadata: PRMetadata) -> None:
        self._metadata = metadata

    async def fetch_pr_metadata(self, pr_number: int) -> PRMetadata:
        return self._metadata

    async def fetch_commits(self, pr_number: int) -> list[Commit]:
        return [Commit(sha="abc1234", message="feat: add auth")]

    async def fetch_changed_files(self, pr_number: int) -> list[ChangedFile]:
        return [ChangedFile(path="src/auth.py", status="modified", additions=10, deletions=2)]

    async def fetch_diff(self, pr_number: int, path: str) -> str:
        return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new"

    async def fetch_file(self, path: str, ref: str) -> str:
        return f"# {path}\n"

    async def fetch_tree(self, ref: str) -> list[str]:
        return ["src/auth.py", "tests/test_auth.py"]

    async def fetch_comments(self, pr_number: int, after: str | None = None) -> list[Comment]:
        return []


class MockCommenter(Commenter):
    def __init__(self) -> None:
        self.posted_comments: list[str] = []
        self.posted_reviews: list[tuple[str, str, list]] = []

    async def post_comment(self, pr_number: int, body: str) -> str:
        self.posted_comments.append(body)
        return "comment-id"

    async def post_review(
        self,
        pr_number: int,
        body: str,
        verdict: Literal["approve", "request_changes", "comment"],
        inline_comments: list[InlineComment],
    ) -> None:
        self.posted_reviews.append((body, verdict, inline_comments))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLIENT_CFG = ClientConfig(provider="anthropic", model="test", api_key="test-key")

_PLAN = {
    "files": ["src/auth.py"],
    "focus": "auth",
    "guidance": "Check for bugs.",
    "tier": "fast",        # keeps Analysis on fast_client — simpler scripting
    "preload_diffs": False,
}


def _make_metadata(draft: bool = False) -> PRMetadata:
    return PRMetadata(
        title="Test PR", description="desc", author="dev",
        base_branch="main", head_branch="feature/x",
        labels=[], pr_number=1, draft=draft,
    )


def _make_config(wiki_root: Path) -> ArgusConfig:
    return ArgusConfig(
        wiki_root=wiki_root,
        bot_username="argus-test",
        fast_client=_CLIENT_CFG,
        standard_client=_CLIENT_CFG,
        deep_client=_CLIENT_CFG,
    )


def _make_app_ctx(
    wiki_root: Path,
    fast: list[LLMResponse] | None = None,
    standard: list[LLMResponse] | None = None,
    deep: list[LLMResponse] | None = None,
) -> ApplicationContext:
    return ApplicationContext(
        clients=ClientTier(
            fast=ScriptedClient(fast or []),
            standard=ScriptedClient(standard or []),
            deep=ScriptedClient(deep or []),
        ),
        wiki_root=wiki_root,
        pr_lock=PRLockManager(),
        bot_username="argus-test",
        config=_make_config(wiki_root),
    )


def _wiring_app_ctx(wiki_root: Path) -> ApplicationContext:
    noop = _NoopClient()
    return ApplicationContext(
        clients=ClientTier(fast=noop, standard=noop, deep=noop),
        wiki_root=wiki_root,
        pr_lock=PRLockManager(),
        bot_username="argus-test",
        config=_make_config(wiki_root),
    )


def _pr_trigger(commenter: MockCommenter, wiki_root: Path, draft: bool = False) -> PROpenedTrigger:
    return PROpenedTrigger(
        installation_id=1, repo="owner/repo", pr_number=1,
        commit_sha="abc1234", actor="dev",
        reader=MockReader(_make_metadata(draft=draft)),
        commenter=commenter,
    )


def _comment_trigger(commenter: MockCommenter, body: str) -> CommentReceivedTrigger:
    return CommentReceivedTrigger(
        installation_id=1, repo="owner/repo", pr_number=1,
        commit_sha="abc1234", actor="dev",
        comment_id="c1", comment_body=body,
        reader=MockReader(_make_metadata()),
        commenter=commenter,
    )


# Response factories
def _triage_resp(plans: list[dict]) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="t1", name="submit_plans", arguments={"plans": plans})],
    )

def _report_resp(findings: str = "No issues.", severity: str = "none") -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(
            id="t2", name="submit_report",
            arguments={"findings": findings, "severity": severity, "inline_suggestions": []},
        )],
    )

def _decision_resp(decision: str) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="t3", name="submit_decision", arguments={"decision": decision})],
    )

def _reply_resp(body: str) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="t4", name="submit_reply", arguments={"body": body})],
    )

def _text_resp(text: str) -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[])


# ---------------------------------------------------------------------------
# Wiring tests
# ---------------------------------------------------------------------------

def test_pr_workflow_entry_is_prload(tmp_path):
    wf = build_pr_workflow(_wiring_app_ctx(tmp_path))
    assert isinstance(wf.entry, PRLoadNode)


def test_pr_workflow_contains_all_expected_node_types(tmp_path):
    wf = build_pr_workflow(_wiring_app_ctx(tmp_path))
    types = {type(n) for n in wf.edges}
    assert types >= {
        PRLoadNode, AcknowledgeNode, MemLoadNode, TreeLoadNode, ThreadLoadNode,
        TriageNode, PassThroughNode, AnalysisNode, SummaryNode,
    }


def test_pr_workflow_wikimerge_is_terminal(tmp_path):
    wf = build_pr_workflow(_wiring_app_ctx(tmp_path))
    assert WikiMergeNode not in {type(n) for n in wf.edges}


def test_pr_workflow_triage_has_conditional_edge(tmp_path):
    wf = build_pr_workflow(_wiring_app_ctx(tmp_path))
    triage = next(n for n in wf.edges if isinstance(n, TriageNode))
    assert isinstance(wf.edges[triage], ConditionalEdge)


def test_pr_workflow_passthrough_fanout_targets_analysis(tmp_path):
    wf = build_pr_workflow(_wiring_app_ctx(tmp_path))
    pass_node = next(n for n in wf.edges if isinstance(n, PassThroughNode))
    edge = wf.edges[pass_node]          # white-box: verifying internal target
    assert isinstance(edge, PipelineStateFanOutEdge)
    assert isinstance(edge._target, AnalysisNode)


def test_pr_workflow_analysis_join_targets_summary(tmp_path):
    wf = build_pr_workflow(_wiring_app_ctx(tmp_path))
    analysis = next(n for n in wf.edges if isinstance(n, AnalysisNode))
    edge = wf.edges[analysis]
    assert isinstance(edge, PipelineStateJoinEdge)
    assert isinstance(edge._target, SummaryNode)


def test_pr_workflow_summary_directs_to_wikimerge(tmp_path):
    wf = build_pr_workflow(_wiring_app_ctx(tmp_path))
    summary = next(n for n in wf.edges if isinstance(n, SummaryNode))
    edge = wf.edges[summary]
    assert isinstance(edge, DirectedEdge)
    assert isinstance(edge._target, WikiMergeNode)


def test_pr_workflow_zero_plan_conditional_routes_to_summary(tmp_path):
    wf = build_pr_workflow(_wiring_app_ctx(tmp_path))
    triage = next(n for n in wf.edges if isinstance(n, TriageNode))
    fn = wf.edges[triage]._fn
    state = PipelineState(payload=PipelinePayload(), data=[])
    pairs = fn(state)
    assert len(pairs) == 1
    node, next_state = pairs[0]
    assert isinstance(node, SummaryNode)
    assert next_state.data == []


def test_pr_workflow_nonzero_plan_conditional_routes_to_passthrough(tmp_path):
    wf = build_pr_workflow(_wiring_app_ctx(tmp_path))
    triage = next(n for n in wf.edges if isinstance(n, TriageNode))
    fn = wf.edges[triage]._fn
    state = PipelineState(payload=PipelinePayload(), data=["plan"])
    pairs = fn(state)
    assert len(pairs) == 1
    node, _ = pairs[0]
    assert isinstance(node, PassThroughNode)


def test_comment_workflow_entry_is_prload(tmp_path):
    wf = build_comment_workflow(_wiring_app_ctx(tmp_path), "hello")
    assert isinstance(wf.entry, PRLoadNode)


def test_comment_workflow_route_has_conditional_edge(tmp_path):
    wf = build_comment_workflow(_wiring_app_ctx(tmp_path), "hello")
    route = next(n for n in wf.edges if isinstance(n, RouteNode))
    assert isinstance(wf.edges[route], ConditionalEdge)


def test_comment_workflow_conversation_is_terminal(tmp_path):
    wf = build_comment_workflow(_wiring_app_ctx(tmp_path), "hello")
    assert ConversationNode not in {type(n) for n in wf.edges}


def test_comment_workflow_route_conditional_conversation_branch(tmp_path):
    wf = build_comment_workflow(_wiring_app_ctx(tmp_path), "hello")
    route = next(n for n in wf.edges if isinstance(n, RouteNode))
    fn = wf.edges[route]._fn
    state = PipelineState(payload=PipelinePayload(), data="conversation")
    pairs = fn(state)
    assert len(pairs) == 1
    node, _ = pairs[0]
    assert isinstance(node, ConversationNode)


def test_comment_workflow_route_conditional_analysis_branch(tmp_path):
    wf = build_comment_workflow(_wiring_app_ctx(tmp_path), "hello")
    route = next(n for n in wf.edges if isinstance(n, RouteNode))
    fn = wf.edges[route]._fn
    state = PipelineState(payload=PipelinePayload(), data="analysis")
    pairs = fn(state)
    assert len(pairs) == 1
    node, next_state = pairs[0]
    assert isinstance(node, TriageNode)
    assert next_state.data is None  # RouteDecision stripped before Triage


# ---------------------------------------------------------------------------
# Behavior tests
# ---------------------------------------------------------------------------

async def test_draft_pr_skips_review(tmp_path):
    commenter = MockCommenter()
    trigger = _pr_trigger(commenter, tmp_path, draft=True)
    await Dispatcher(_make_app_ctx(tmp_path)).dispatch(trigger)
    assert len(commenter.posted_reviews) == 0
    assert any("Draft" in c for c in commenter.posted_comments)


async def test_pr_zero_plans_approves(tmp_path):
    commenter = MockCommenter()
    trigger = _pr_trigger(commenter, tmp_path)
    app_ctx = _make_app_ctx(
        tmp_path,
        fast=[_triage_resp([])],
        standard=[_text_resp("Trivial changes, no issues.")],
    )
    await Dispatcher(app_ctx).dispatch(trigger)
    assert len(commenter.posted_reviews) == 1
    _, verdict, _ = commenter.posted_reviews[0]
    assert verdict == "approve"


async def test_pr_one_plan_severity_none_approves(tmp_path):
    commenter = MockCommenter()
    trigger = _pr_trigger(commenter, tmp_path)
    app_ctx = _make_app_ctx(
        tmp_path,
        fast=[_triage_resp([_PLAN]), _report_resp("No issues.", "none")],
        standard=[_text_resp("LGTM.")],
    )
    await Dispatcher(app_ctx).dispatch(trigger)
    assert len(commenter.posted_reviews) == 1
    body, verdict, inline = commenter.posted_reviews[0]
    assert verdict == "approve"
    assert body == "LGTM."
    assert inline == []


async def test_pr_high_severity_requests_changes(tmp_path):
    commenter = MockCommenter()
    trigger = _pr_trigger(commenter, tmp_path)
    app_ctx = _make_app_ctx(
        tmp_path,
        fast=[_triage_resp([_PLAN]), _report_resp("SQL injection found.", "high")],
        standard=[_text_resp("Critical issue found.")],
    )
    await Dispatcher(app_ctx).dispatch(trigger)
    _, verdict, _ = commenter.posted_reviews[0]
    assert verdict == "request_changes"


async def test_pr_medium_severity_below_default_threshold_comments(tmp_path):
    commenter = MockCommenter()
    trigger = _pr_trigger(commenter, tmp_path)
    # default threshold = "high"; medium < high → "comment"
    app_ctx = _make_app_ctx(
        tmp_path,
        fast=[_triage_resp([_PLAN]), _report_resp("Missing null check.", "medium")],
        standard=[_text_resp("Minor issue noted.")],
    )
    await Dispatcher(app_ctx).dispatch(trigger)
    _, verdict, _ = commenter.posted_reviews[0]
    assert verdict == "comment"


async def test_comment_conversation_branch_posts_reply(tmp_path):
    commenter = MockCommenter()
    trigger = _comment_trigger(commenter, "What does this function do?")
    app_ctx = _make_app_ctx(
        tmp_path,
        fast=[_decision_resp("conversation")],
        standard=[_reply_resp("It validates the JWT token.")],
    )
    await Dispatcher(app_ctx).dispatch(trigger)
    assert len(commenter.posted_reviews) == 0
    assert any("validates the JWT token" in c for c in commenter.posted_comments)


async def test_comment_analysis_branch_zero_plans_approves(tmp_path):
    commenter = MockCommenter()
    trigger = _comment_trigger(commenter, "@argus re-review")
    app_ctx = _make_app_ctx(
        tmp_path,
        fast=[_decision_resp("analysis"), _triage_resp([])],
        standard=[_text_resp("Nothing to flag.")],
    )
    await Dispatcher(app_ctx).dispatch(trigger)
    assert len(commenter.posted_reviews) == 1
    _, verdict, _ = commenter.posted_reviews[0]
    assert verdict == "approve"
