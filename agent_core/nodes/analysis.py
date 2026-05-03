"""Analysis-phase nodes: Triage, Route, Analysis.

Triage and Route use single direct client.complete() calls with tool-forcing
(submit_plans / submit_decision). Analysis runs a manual ReAct loop so it can
detect submit_report and stop immediately without an extra round-trip.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from llm_framework import BoundTool, LLMClient, Message, ToolResult
from llm_framework.tools import ToolCall
from llm_framework.workflow import Node

from ..context import ClientTier, PipelineState, get_session
from ..domain import AnalysisReport, InlineSuggestion, Plan, RouteDecision
from ..tools.code import make_code_tools
from ..tools.memory import make_analysis_memory_tools

logger = logging.getLogger(__name__)

_MAX_ANALYSIS_ITERATIONS = 20

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_WIKI_WRITE_GUIDE = """\
Wiki write guide:
- Read the wiki index (already in your context) before writing — update an existing page
  rather than creating a duplicate.
- Use memory_append to add findings to an existing page; memory_write to create a new one
  or fully replace stale content.
- Naming: module-{name}.md · users/{username}.md · domain.md
- Add this frontmatter to every new page you create:
    ---
    last_updated_pr: <PR number>
    last_updated_sha: <commit SHA>
    ---
- Keep the index entry ≤ 150 characters and specific enough to answer:
  "Is this page relevant to what I'm currently reviewing?"\
"""

_TRIAGE_SYSTEM = """\
You are Triage for Argus, an automated code reviewer.

Analyze the Pull Request and call submit_plans with a list of analysis workstreams.
Each workstream groups related files and provides focused guidance for an Analysis agent.

Rules:
- Group related files together (by module, feature, or concern — no cross-group overlap).
- Sort plans by risk descending (security > correctness > performance).
- You are allowed to skip files if suitable, for example, generated file.
- For trivial PRs (docs-only, formatting, no logic changes) submit an empty list.
- Never read file contents — work only from the file list, patch stats, and PR metadata.

You MUST call submit_plans. Do not respond with text.\
"""

_ROUTE_SYSTEM = """\
You are a routing agent for Argus. Classify the incoming PR comment as requiring
'conversation' or 'analysis'.

  conversation — the comment asks a question, wants clarification, or starts a discussion.
  analysis     — the comment requests a re-review, asks Argus to inspect specific code,
                 or accompanies a new push.

Default to 'conversation' when uncertain.

You MUST call submit_decision. Do not respond with text.\
"""

_ANALYSIS_SYSTEM = f"""\
You are an Analysis agent for Argus, a code reviewer. You investigate an assigned set
of files in a Pull Request and produce a structured report.

Workflow:
1. Read the PR context, commits, and your assigned focus and guidance.
2. Use tools to navigate the codebase — read diffs, open files, search for usages.
3. Consult the memory wiki for relevant codebase context before drawing conclusions.
4. Write memory wiki observations only when you discover something a future reviewer could not
   re-derive from the current code in 30 seconds. Skip trivial or visible-in-code facts.
4b. If you believe current structure descriptions is blank (TODO), in-complete, incorrect,
    and you have knowledge to improve it, call
    structure_upsert(module, description, evidence_paths) to improve module descriptions.
4c. Skip structure_upsert when evidence is weak; never guess architecture from names alone.
5. Call submit_report as your FINAL action with all findings. Make no tool calls after it.

{_WIKI_WRITE_GUIDE}

Severity guide:
  critical — data loss, security vulnerability, crash in the happy path
  high     — logic bug, broken contract, significant performance regression
  medium   — risky pattern, missing error handling on external calls
  low      — style issue, minor improvement
  none     — no issues found\
"""

# ---------------------------------------------------------------------------
# Capture tools (single-use, stateful)
# ---------------------------------------------------------------------------

class _SubmitPlansTool(BoundTool):
    def __init__(self, max_plans: int) -> None:
        super().__init__(
            name="submit_plans",
            description=f"Submit the analysis plans (max {max_plans}).",
            schema={
                "type": "object",
                "properties": {
                    "plans": {
                        "type": "array",
                        "maxItems": max_plans,
                        "items": {
                            "type": "object",
                            "properties": {
                                "files": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "File paths assigned to this workstream.",
                                },
                                "focus": {
                                    "type": "string",
                                    "description": "Short label, e.g. 'auth security'.",
                                },
                                "guidance": {
                                    "type": "string",
                                    "description": "Directive prompt for the Analysis agent.",
                                },
                                "tier": {
                                    "type": "string",
                                    "enum": ["fast", "standard", "deep"],
                                },
                                "preload_diffs": {
                                    "type": "boolean",
                                    "description": "True when total diff lines for these files is small.",
                                },
                            },
                            "required": ["files", "focus", "guidance", "tier", "preload_diffs"],
                        },
                    }
                },
                "required": ["plans"],
            },
        )
        self.captured: list[dict] | None = None

    async def process(self, arguments: dict) -> str:
        self.captured = arguments.get("plans", [])
        return "Plans received."


class _SubmitDecisionTool(BoundTool):
    def __init__(self) -> None:
        super().__init__(
            name="submit_decision",
            description="Submit the routing decision.",
            schema={
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": ["conversation", "analysis"],
                    }
                },
                "required": ["decision"],
            },
        )
        self.captured: str | None = None

    async def process(self, arguments: dict) -> str:
        self.captured = arguments.get("decision", "conversation")
        return "Decision received."


class _SubmitReportTool(BoundTool):
    def __init__(self) -> None:
        super().__init__(
            name="submit_report",
            description=(
                "Submit the completed analysis report. Call this as your final action."
            ),
            schema={
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "string",
                        "description": "Overall findings narrative for this workstream.",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "none"],
                        "description": "Highest severity across all findings.",
                    },
                    "inline_suggestions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "start_line": {"type": "integer"},
                                "end_line": {"type": "integer"},
                                "body": {
                                    "type": "string",
                                    "description": "Markdown comment body.",
                                },
                                "severity": {
                                    "type": "string",
                                    "enum": ["critical", "high", "medium", "low"],
                                },
                            },
                            "required": ["path", "start_line", "end_line", "body", "severity"],
                        },
                    },
                },
                "required": ["findings", "severity"],
            },
        )
        self.captured: dict | None = None

    async def process(self, arguments: dict) -> str:
        self.captured = arguments
        return "Report submitted. Do not make any more tool calls."


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------

class TriageNode(Node):
    """Produces list[Plan] from PR context via a single structured LLM call."""

    def __init__(
        self,
        fast_client: LLMClient,
        max_plans: int,
        preload_diff_threshold: int,
    ) -> None:
        self._client = fast_client
        self._max_plans = max_plans
        self._preload_diff_threshold = preload_diff_threshold

    async def execute(self, state: PipelineState[None]) -> PipelineState[list[Plan]]:
        ctx = get_session()
        key = _run_key(ctx)
        logger.info("%s node=Triage start", key)
        submit = _SubmitPlansTool(self._max_plans)

        prompt = _build_triage_prompt(ctx.metadata, state.payload, self._preload_diff_threshold)
        response = await self._client.complete(
            messages=[Message.text("user", prompt)],
            tools=[submit],
            system=_TRIAGE_SYSTEM,
        )

        plans: list[Plan] = []
        # Process any tool calls in the response
        for call in response.tool_calls:
            if call.name == "submit_plans":
                await submit.process(call.arguments)

        if submit.captured is not None:
            raw_plans = submit.captured[: self._max_plans]
            plans = _parse_plans(raw_plans)
        else:
            logger.warning("Triage did not call submit_plans — returning empty plan list")
        tiers = {"fast": 0, "standard": 0, "deep": 0}
        preload_count = 0
        for p in plans:
            tiers[p.tier] += 1
            if p.preload_diffs:
                preload_count += 1
        logger.info(
            "%s node=Triage done plans=%d tiers=fast:%d,standard:%d,deep:%d preload_diffs=%d",
            key,
            len(plans),
            tiers["fast"],
            tiers["standard"],
            tiers["deep"],
            preload_count,
        )

        return PipelineState(payload=state.payload, data=plans)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

class RouteNode(Node):
    """Classifies a comment as 'conversation' or 'analysis' via a single LLM call."""

    def __init__(self, fast_client: LLMClient, comment_body: str) -> None:
        self._client = fast_client
        self._comment_body = comment_body

    async def execute(self, state: PipelineState[None]) -> PipelineState[RouteDecision]:
        submit = _SubmitDecisionTool()
        ctx = get_session()
        key = _run_key(ctx)
        logger.info("%s node=Route start", key)

        recent_snippet = "\n".join(
            f"  [{c.author}]: {c.body[:120]}"
            for c in state.payload.recent_comments[-5:]
        )
        prompt = (
            f"**PR title:** {ctx.metadata.title}\n\n"
            f"**Triggering comment:**\n{self._comment_body}\n\n"
            + (f"**Recent thread:**\n{recent_snippet}\n" if recent_snippet else "")
        )

        response = await self._client.complete(
            messages=[Message.text("user", prompt)],
            tools=[submit],
            system=_ROUTE_SYSTEM,
        )

        for call in response.tool_calls:
            if call.name == "submit_decision":
                await submit.process(call.arguments)

        decision: RouteDecision = submit.captured or "conversation"  # type: ignore[assignment]
        if submit.captured is None:
            logger.warning("Route did not call submit_decision — defaulting to 'conversation'")
        logger.info("%s node=Route done decision=%s", key, decision)

        return PipelineState(payload=state.payload, data=decision)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

class AnalysisNode(Node):
    """Runs a ReAct loop to investigate assigned files and produce an AnalysisReport.

    Constructed once; execute() is called concurrently per fan-out branch.
    Each call generates a fresh run_id and wraps memory tools independently.
    """

    def __init__(
        self,
        clients: ClientTier,
        wiki_root: Path,
        max_file_window_lines: int,
        max_search_results: int,
    ) -> None:
        self._clients = clients
        self._wiki_root = wiki_root
        self._max_file_window_lines = max_file_window_lines
        self._max_search_results = max_search_results

    async def execute(self, state: PipelineState[Plan]) -> PipelineState[AnalysisReport]:
        plan = state.data
        ctx = get_session()
        key = _run_key(ctx)
        run_id = uuid.uuid4().hex[:8]
        logger.info(
            "%s node=Analysis start run_id=%s focus=%r tier=%s files=%d preload_diffs=%s",
            key,
            run_id,
            plan.focus,
            plan.tier,
            len(plan.files),
            plan.preload_diffs,
        )

        client = {
            "fast": self._clients.fast,
            "standard": self._clients.standard,
            "deep": self._clients.deep,
        }[plan.tier]

        code_tools = make_code_tools(
            state.payload.file_tree,
            self._max_file_window_lines,
            self._max_search_results,
        )
        memory_tools = make_analysis_memory_tools(
            self._wiki_root, ctx.pr_number, run_id
        )
        submit_report = _SubmitReportTool()
        tool_index: dict[str, BoundTool] = {
            t.name: t for t in code_tools + memory_tools + [submit_report]
        }

        # Preload diffs if the plan says to
        preloaded_diffs = ""
        if plan.preload_diffs:
            preloaded_diffs = await _fetch_preloaded_diffs(ctx, plan)

        initial_msg = _build_analysis_message(ctx, state.payload, plan, preloaded_diffs)
        history = [Message.text("user", initial_msg)]

        try:
            for _ in range(_MAX_ANALYSIS_ITERATIONS):
                response = await client.complete(
                    messages=history,
                    tools=list(tool_index.values()),
                    system=_ANALYSIS_SYSTEM,
                )

                if not response.tool_calls:
                    break

                history.append(response.to_message())
                results = list(
                    await asyncio.gather(*[
                        _dispatch_tool(tool_index, call)
                        for call in response.tool_calls
                    ])
                )
                history.append(Message.from_tool_results(results))

                if submit_report.captured is not None:
                    break  # agent submitted — stop immediately

            report = (
                _parse_report(plan, submit_report.captured)
                if submit_report.captured is not None
                else AnalysisReport(
                    plan=plan,
                    findings=response.content or "(no findings submitted)",
                    severity="none",
                )
            )

        except Exception as exc:
            logger.exception("Analysis failed for focus=%r: %s", plan.focus, exc)
            report = AnalysisReport(
                plan=plan, findings="", severity="none", failed=True, error=str(exc)
            )
        logger.info(
            "%s node=Analysis done run_id=%s focus=%r severity=%s inline_suggestions=%d failed=%s",
            key,
            run_id,
            plan.focus,
            report.severity,
            len(report.inline_suggestions),
            report.failed,
        )

        return PipelineState(
            payload=state.payload,
            data=report,
            fan_n=state.fan_n,
            fan_i=state.fan_i,
        )


def _run_key(ctx: Any) -> str:
    return f"{ctx.repo}:{ctx.pr_number}"


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_triage_prompt(metadata: Any, payload: Any, preload_threshold: int) -> str:
    commits_text = "\n".join(
        f"- {c.sha[:7]}: {c.message}" for c in payload.commits
    ) or "(none)"

    files_text = "\n".join(
        f"- {f.path}  [{f.status}]  +{f.additions}/-{f.deletions}"
        for f in payload.changed_files
    ) or "(none)"

    total_diff_lines = sum(
        f.additions + f.deletions for f in payload.changed_files
    )
    preload_note = (
        f"(total diff lines: {total_diff_lines} — "
        + ("set preload_diffs=true" if total_diff_lines < preload_threshold else "set preload_diffs=false")
        + ")"
    )

    sections = [
        f"## PR #{metadata.pr_number}: {metadata.title}",
        f"**Author:** {metadata.author}  |  **Branch:** {metadata.head_branch} → {metadata.base_branch}",
        f"**Draft:** {metadata.draft}",
        "",
        "**Description:**",
        metadata.description or "(none)",
        "",
        "## Commits",
        commits_text,
        "",
        f"## Changed Files ({len(payload.changed_files)} files) {preload_note}",
        files_text,
    ]
    if payload.sync_base_sha and payload.sync_changed_files:
        sync_files_text = "\n".join(
            f"- {f.path}  [{f.status}]  +{f.additions}/-{f.deletions}"
            for f in payload.sync_changed_files
        )
        sections += [
            "",
            f"## New Delta Since Last Sync ({len(payload.sync_changed_files)} files)",
            f"Base: `{payload.sync_base_sha[:12]}`",
            sync_files_text,
            "",
            "Prioritize this delta when deciding whether this run adds new findings or only revalidates prior conclusions.",
        ]

    if payload.wiki_index:
        sections += ["", "## Wiki Index", payload.wiki_index]
    if payload.wiki_structure:
        sections += ["", "## Codebase Structure", payload.wiki_structure]

    return "\n".join(sections)


def _build_analysis_message(ctx: Any, payload: Any, plan: Plan, preloaded_diffs: str) -> str:
    commits_text = "\n".join(
        f"- {c.sha[:7]}: {c.message}" for c in payload.commits
    ) or "(none)"

    sections = [
        f"## PR #{ctx.pr_number}: {ctx.metadata.title}",
        f"**Author:** {ctx.metadata.author}  |  **Branch:** {ctx.metadata.head_branch}",
        "",
        "## Commits",
        commits_text,
        "",
        "## Your Assignment",
        f"**Focus:** {plan.focus}",
        f"**Files:** {', '.join(plan.files)}",
        f"**Guidance:** {plan.guidance}",
    ]

    if payload.wiki_index:
        sections += ["", "## Wiki Index", payload.wiki_index]
    if payload.wiki_structure:
        sections += ["", "## Codebase Structure", payload.wiki_structure]

    if payload.thread_summary:
        sections += ["", "## PR Thread Summary", payload.thread_summary]
    if payload.recent_comments:
        recent = "\n".join(
            f"[{c.author}]: {c.body}" for c in payload.recent_comments
        )
        sections += ["", "## Recent Comments", recent]

    if preloaded_diffs:
        sections += ["", "## Pre-loaded Diffs", preloaded_diffs]

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_preloaded_diffs(ctx: Any, plan: Plan) -> str:
    parts: list[str] = []
    for path in plan.files:
        try:
            diff = await ctx.reader.fetch_diff(ctx.pr_number, path)
            parts.append(f"### {path}\n```diff\n{diff}\n```")
        except Exception as exc:
            logger.debug("Could not preload diff for %r: %s", path, exc)
    return "\n\n".join(parts)


async def _dispatch_tool(tool_index: dict[str, BoundTool], call: ToolCall) -> ToolResult:
    tool = tool_index.get(call.name)
    if tool is None:
        return ToolResult(
            id=call.id,
            content=f"Unknown tool {call.name!r}. Available: {list(tool_index)!r}",
            is_error=True,
        )
    try:
        content = await tool.process(call.arguments)
        return ToolResult(id=call.id, content=content)
    except Exception as exc:
        logger.warning("Tool %r raised: %s", call.name, exc)
        return ToolResult(id=call.id, content=str(exc), is_error=True)


def _parse_plans(raw_plans: list[dict]) -> list[Plan]:
    plans: list[Plan] = []
    for i, raw in enumerate(raw_plans):
        try:
            files = raw.get("files")
            focus = raw.get("focus")
            guidance = raw.get("guidance")
            tier = raw.get("tier")
            preload_diffs = raw.get("preload_diffs")

            if (
                not isinstance(files, list)
                or not all(isinstance(f, str) for f in files)
                or not isinstance(focus, str)
                or not isinstance(guidance, str)
                or tier not in {"fast", "standard", "deep"}
                or not isinstance(preload_diffs, bool)
            ):
                logger.warning("Dropping invalid triage plan at index %d: %r", i, raw)
                continue

            plans.append(
                Plan(
                    files=files,
                    focus=focus,
                    guidance=guidance,
                    tier=tier,
                    preload_diffs=preload_diffs,
                )
            )
        except Exception:
            logger.warning("Dropping malformed triage plan at index %d", i, exc_info=True)
    return plans




def _parse_report(plan: Plan, raw: dict) -> AnalysisReport:
    inline = [
        InlineSuggestion(
            path=s["path"],
            start_line=s["start_line"],
            end_line=s["end_line"],
            body=s["body"],
            severity=s["severity"],
        )
        for s in raw.get("inline_suggestions", [])
    ]
    return AnalysisReport(
        plan=plan,
        findings=raw["findings"],
        inline_suggestions=inline,
        severity=raw["severity"],
    )
