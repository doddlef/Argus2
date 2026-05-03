"""Pipeline factory functions.

Each function builds and returns a fresh Workflow per invocation. A fresh instance
is required on every event because PipelineStateJoinEdge accumulates state in memory
and is not safe to reuse across concurrent runs.
"""

from __future__ import annotations

from llm_framework.edges import ConditionalEdge, DirectedEdge
from llm_framework.workflow import Workflow

from .context import ApplicationContext, PipelineState
from .edges import PassThroughNode, PipelineStateFanOutEdge, PipelineStateJoinEdge
from .nodes.analysis import AnalysisNode, RouteNode, TriageNode
from .nodes.conversation import ConversationNode
from .nodes.loading import (
    AcknowledgeNode,
    MemLoadNode,
    PRLoadNode,
    StructureSyncNode,
    ThreadLoadNode,
    TreeLoadNode,
)
from .nodes.output import SummaryNode, WikiMergeNode

_ACK_PR = "Argus is reviewing this PR. I'll post my findings shortly."
_ACK_COMMENT = "On it — I'll respond shortly."


def build_pr_workflow(app_ctx: ApplicationContext) -> Workflow:
    """Build the PR-opened / PR-sync pipeline."""
    cfg = app_ctx.config

    # Loading phase
    prload = PRLoadNode()
    acknowledge = AcknowledgeNode(message=_ACK_PR, enabled=cfg.acknowledge_events)
    treeload = TreeLoadNode()
    structure_sync = StructureSyncNode(
        wiki_root=app_ctx.wiki_root,
        enabled=cfg.structure_sync_enabled,
    )
    memload = MemLoadNode(app_ctx.wiki_root)
    threadload = ThreadLoadNode(
        wiki_root=app_ctx.wiki_root,
        fast_client=app_ctx.clients.fast,
        threshold=cfg.thread_compact_threshold,
        bot_username=app_ctx.bot_username,
    )

    # Analysis phase
    triage = TriageNode(
        fast_client=app_ctx.clients.fast,
        max_plans=cfg.max_plans,
        preload_diff_threshold=cfg.preload_diff_threshold,
    )
    analysis = AnalysisNode(
        clients=app_ctx.clients,
        wiki_root=app_ctx.wiki_root,
        max_file_window_lines=cfg.max_file_window_lines,
        max_search_results=cfg.max_search_results,
    )

    # Output phase
    summary = SummaryNode(
        standard_client=app_ctx.clients.standard,
        wiki_root=app_ctx.wiki_root,
        max_inline_suggestions=cfg.max_inline_suggestions,
        verdict_threshold=cfg.verdict_threshold,
    )
    wiki_merge = WikiMergeNode(
        standard_client=app_ctx.clients.standard,
        wiki_root=app_ctx.wiki_root,
    )

    pass_node = PassThroughNode()

    def _after_triage(state: PipelineState) -> list:
        if not state.data:
            # 0-plan bypass: trivial PR — approve directly
            return [(summary, PipelineState(payload=state.payload, data=[]))]
        return [(pass_node, state)]

    return Workflow(
        entry=prload,
        edges={
            prload:     DirectedEdge(acknowledge),
            acknowledge: DirectedEdge(treeload),
            treeload:   DirectedEdge(structure_sync),
            structure_sync: DirectedEdge(memload),
            memload:    DirectedEdge(threadload),
            threadload: DirectedEdge(triage),
            triage:     ConditionalEdge(_after_triage),
            pass_node:  PipelineStateFanOutEdge(analysis),
            analysis:   PipelineStateJoinEdge(summary),
            summary:    DirectedEdge(wiki_merge),
            # wiki_merge: terminal — no edge entry
        },
    )


def build_comment_workflow(
    app_ctx: ApplicationContext, comment_body: str
) -> Workflow:
    """Build the comment-received pipeline."""
    cfg = app_ctx.config

    # Loading phase
    prload = PRLoadNode()
    acknowledge = AcknowledgeNode(message=_ACK_COMMENT, enabled=cfg.acknowledge_events)
    treeload = TreeLoadNode()
    structure_sync = StructureSyncNode(
        wiki_root=app_ctx.wiki_root,
        enabled=cfg.structure_sync_enabled,
    )
    memload = MemLoadNode(app_ctx.wiki_root)
    threadload = ThreadLoadNode(
        wiki_root=app_ctx.wiki_root,
        fast_client=app_ctx.clients.fast,
        threshold=cfg.thread_compact_threshold,
        bot_username=app_ctx.bot_username,
    )

    # Route
    route = RouteNode(fast_client=app_ctx.clients.fast, comment_body=comment_body)

    # Conversation branch (terminal)
    conversation = ConversationNode(
        standard_client=app_ctx.clients.standard,
        wiki_root=app_ctx.wiki_root,
        max_file_window_lines=cfg.max_file_window_lines,
        max_search_results=cfg.max_search_results,
        comment_body=comment_body,
    )

    # Analysis branch (mirrors PR workflow)
    triage = TriageNode(
        fast_client=app_ctx.clients.fast,
        max_plans=cfg.max_plans,
        preload_diff_threshold=cfg.preload_diff_threshold,
    )
    analysis = AnalysisNode(
        clients=app_ctx.clients,
        wiki_root=app_ctx.wiki_root,
        max_file_window_lines=cfg.max_file_window_lines,
        max_search_results=cfg.max_search_results,
    )
    summary = SummaryNode(
        standard_client=app_ctx.clients.standard,
        wiki_root=app_ctx.wiki_root,
        max_inline_suggestions=cfg.max_inline_suggestions,
        verdict_threshold=cfg.verdict_threshold,
    )
    wiki_merge = WikiMergeNode(
        standard_client=app_ctx.clients.standard,
        wiki_root=app_ctx.wiki_root,
    )

    pass_node = PassThroughNode()

    def _after_route(state: PipelineState) -> list:
        if state.data == "conversation":
            return [(conversation, state)]
        # "analysis" path: strip RouteDecision from data before Triage
        return [(triage, PipelineState(payload=state.payload, data=None))]

    def _after_triage(state: PipelineState) -> list:
        if not state.data:
            return [(summary, PipelineState(payload=state.payload, data=[]))]
        return [(pass_node, state)]

    return Workflow(
        entry=prload,
        edges={
            prload:      DirectedEdge(acknowledge),
            acknowledge: DirectedEdge(treeload),
            treeload:    DirectedEdge(structure_sync),
            structure_sync: DirectedEdge(memload),
            memload:     DirectedEdge(threadload),
            threadload:  DirectedEdge(route),
            route:       ConditionalEdge(_after_route),
            # conversation: terminal
            triage:      ConditionalEdge(_after_triage),
            pass_node:   PipelineStateFanOutEdge(analysis),
            analysis:    PipelineStateJoinEdge(summary),
            summary:     DirectedEdge(wiki_merge),
            # wiki_merge: terminal
        },
    )
