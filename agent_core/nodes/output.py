"""Output-phase nodes: Summary and WikiMerge.

SummaryNode: synthesises AnalysisReports → posts GitHub review (rule-based verdict).
WikiMergeNode: ReAct agent applying tmp/ operation files to the canonical wiki.

Failure policy:
  SummaryNode   — post static error comment + clean up tmp/pr{n}-* files; does not re-raise
  WikiMergeNode — log error; tmp files preserved for self-healing on next pipeline run
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal

from llm_framework import BoundTool, LLMClient, Message, ToolResult
from llm_framework.tools import ToolCall
from llm_framework.workflow import Node

from ..context import PipelineState, get_session
from ..domain import AnalysisReport, InlineComment, InlineSuggestion
from ..tools.memory import make_memory_tools

logger = logging.getLogger(__name__)

_SEVERITY_RANK: dict[str, int] = {
    "critical": 4, "high": 3, "medium": 2, "low": 1, "none": 0,
}
_RANK_TO_SEVERITY: dict[int, str] = {v: k for k, v in _SEVERITY_RANK.items()}

_MAX_WIKI_MERGE_ITERATIONS = 20
_STRUCTURE_PATH = "structure.md"

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM = """\
You are the Summary agent for Argus, a code reviewer. Write a concise, actionable
GitHub PR review body based on the analysis reports provided.

Guidelines:
- Synthesise findings across workstreams into a cohesive narrative.
- Surface cross-cutting patterns ("all auth changes are missing input validation").
- For failed workstreams, add a brief note: "Analysis of <focus> failed — manual review recommended."
- Use the thread summary and recent comments to avoid re-flagging already-resolved issues.
- Do NOT describe inline suggestions in the body — they are posted separately.
- The overall verdict has been pre-determined; your job is only to write the body text.
- Output only the review body Markdown. No preamble, no trailing commentary.\
"""

_WIKI_MERGE_SYSTEM = """\
You are the WikiMerge agent for Argus. Apply staged analysis observations to the
canonical wiki, ensuring consistency and avoiding duplication.

The existing content of each target page has been pre-loaded in the initial message.
You do NOT need to call memory_read for the target pages listed there.
Use memory_read only if you need to consult a related wiki page not listed below.

Structure handling:
- `structure.md` semantic module updates are staged as structured `structure_upsert` ops
  and applied outside this tool loop.
- Do not rewrite architecture guidance in `structure.md` via memory_write/memory_append
  unless explicitly required for non-structured recovery.
- Preserve human-authored architecture sections (`Modules`, `Runtime Flow`,
  `Conventions`) as stable guidance.

Process for each target page:
1. Read the pre-loaded existing content and all staged operations for that page.
2. Merge: combine insights, resolve contradictions, drop stale items.
3. Apply write discipline — discard anything a reviewer could derive from current code in 30 seconds.
4. Call memory_write to replace stale content, or memory_append to extend good content.
5. After processing all pages, stop. Make no further tool calls.

The description parameter on memory_write/memory_append becomes the index.md entry — make it
specific enough to answer "is this relevant to what I'm reviewing now?" (≤ 150 characters).

Output nothing. Use only tools.\
"""


# ---------------------------------------------------------------------------
# SummaryNode
# ---------------------------------------------------------------------------

class SummaryNode(Node):
    """Synthesises AnalysisReports and posts the GitHub PR review.

    Verdict is determined by rule-based logic (not LLM). The LLM writes
    the review body narrative given the pre-computed verdict.
    """

    def __init__(
        self,
        standard_client: LLMClient,
        wiki_root: Path,
        max_inline_suggestions: int,
        verdict_threshold: Literal["critical", "high", "medium"],
    ) -> None:
        self._client = standard_client
        self._wiki_root = wiki_root
        self._max_inline = max_inline_suggestions
        self._threshold = verdict_threshold

    async def execute(self, state: PipelineState[list[AnalysisReport]]) -> None:
        ctx = get_session()
        key = _run_key(ctx)
        logger.info("%s node=Summary start reports=%d", key, len(state.data))
        reports: list[AnalysisReport] = state.data

        verdict = _compute_verdict(reports, self._threshold)
        inline_suggestions, total_suggestions = _gather_inline_suggestions(
            reports, self._max_inline
        )
        inline_comments = [
            InlineComment(
                path=s.path, start_line=s.start_line, end_line=s.end_line, body=s.body
            )
            for s in inline_suggestions
        ]

        try:
            prompt = _build_summary_prompt(
                ctx.metadata,
                state.payload,
                reports,
                verdict,
                total_suggestions,
                len(inline_comments),
            )
            response = await self._client.complete(
                messages=[Message.text("user", prompt)],
                tools=None,
                system=_SUMMARY_SYSTEM,
            )
            body = response.content or "(Argus encountered an error generating the review body.)"
        except Exception as exc:
            logger.exception("Summary LLM call failed: %s", exc)
            await _post_error_and_cleanup(
                ctx,
                self._wiki_root,
                f"Argus encountered an error generating the review: {exc}",
            )
            return

        try:
            await ctx.commenter.post_review(ctx.pr_number, body, verdict, inline_comments)
            logger.info(
                "%s node=Summary done verdict=%s inline_comments=%d total_inline_suggestions=%d",
                key,
                verdict,
                len(inline_comments),
                total_suggestions,
            )
        except Exception as exc:
            logger.exception("Summary post_review failed: %s", exc)
            await _post_error_and_cleanup(
                ctx,
                self._wiki_root,
                f"Argus generated a review but failed to post it: {exc}",
            )


# ---------------------------------------------------------------------------
# WikiMergeNode
# ---------------------------------------------------------------------------

class WikiMergeNode(Node):
    """ReAct agent that applies Analysis operation files to the canonical wiki.

    Scans tmp/pr{n}-* for staged operations from the current run and any prior
    failed runs (self-healing). Deletes files only on successful completion.
    """

    def __init__(self, standard_client: LLMClient, wiki_root: Path) -> None:
        self._client = standard_client
        self._wiki_root = wiki_root

    async def execute(self, _state: Any) -> None:
        ctx = get_session()
        key = _run_key(ctx)
        logger.info("%s node=WikiMerge start", key)
        owner, repo_name = ctx.repo.split("/", 1)
        project = self._wiki_root / "argus" / owner / repo_name

        all_op_files = _scan_operation_files(project, ctx.pr_number)
        if not all_op_files:
            logger.info("%s node=WikiMerge done op_files=0", key)
            return

        structure_ops, memory_ops = _split_structure_ops(all_op_files)
        if structure_ops:
            _apply_structure_upserts(project, structure_ops, ctx.commit_sha)

        op_files = memory_ops
        if not op_files:
            _delete_op_files(all_op_files)
            logger.info("%s node=WikiMerge done op_files=0 structure_ops=%d", key, len(structure_ops))
            return

        # Pre-load existing canonical pages so the agent skips memory_read round-trips.
        targets = {
            _parse_op_file(f.read_text(encoding="utf-8"))[0]
            for f in op_files
        }
        existing_pages: dict[str, str] = {
            target: (project / target).read_text(encoding="utf-8")
            for target in targets
            if (project / target).exists()
        }

        initial_msg = _build_merge_message(ctx.pr_number, op_files, existing_pages)
        history = [Message.text("user", initial_msg)]
        memory_tools = make_memory_tools(self._wiki_root)
        tool_index = {t.name: t for t in memory_tools}

        try:
            for _ in range(_MAX_WIKI_MERGE_ITERATIONS):
                response = await self._client.complete(
                    messages=history,
                    tools=memory_tools,
                    system=_WIKI_MERGE_SYSTEM,
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

            _delete_op_files(all_op_files)
            logger.info(
                "%s node=WikiMerge done op_files=%d structure_ops=%d targets=%d",
                key,
                len(op_files),
                len(structure_ops),
                len(targets),
            )

        except Exception as exc:
            logger.warning(
                "WikiMerge failed — operation files preserved for next run: %s", exc
            )


def _run_key(ctx: Any) -> str:
    return f"{ctx.repo}:{ctx.pr_number}"


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def _compute_verdict(
    reports: list[AnalysisReport],
    threshold: Literal["critical", "high", "medium"],
) -> Literal["approve", "request_changes", "comment"]:
    effective = _effective_severity(reports)
    if effective == "none":
        return "approve"
    if _SEVERITY_RANK[effective] >= _SEVERITY_RANK[threshold]:
        return "request_changes"
    return "comment"


def _effective_severity(
    reports: list[AnalysisReport],
) -> Literal["critical", "high", "medium", "low", "none"]:
    rank = 0
    for report in reports:
        if report.failed:
            continue
        rank = max(rank, _SEVERITY_RANK.get(report.severity, 0))
        for s in report.inline_suggestions:
            rank = max(rank, _SEVERITY_RANK.get(s.severity, 0))
    return _RANK_TO_SEVERITY[rank]  # type: ignore[return-value]


def _gather_inline_suggestions(
    reports: list[AnalysisReport],
    max_inline: int,
) -> tuple[list[InlineSuggestion], int]:
    all_suggestions = [
        s
        for report in reports
        if not report.failed
        for s in report.inline_suggestions
    ]
    all_suggestions.sort(
        key=lambda s: _SEVERITY_RANK.get(s.severity, 0), reverse=True
    )
    return all_suggestions[:max_inline], len(all_suggestions)


def _build_summary_prompt(
    metadata: Any,
    payload: Any,
    reports: list[AnalysisReport],
    verdict: str,
    total_suggestions: int,
    included_suggestions: int,
) -> str:
    sections = [
        f"## PR #{metadata.pr_number}: {metadata.title}",
        f"**Author:** {metadata.author}  |  **Branch:** {metadata.head_branch} → {metadata.base_branch}",
        f"**Verdict (pre-determined):** {verdict}",
        "",
    ]

    if payload.thread_summary:
        sections += ["## Thread Summary (for continuity)", payload.thread_summary, ""]
    if payload.recent_comments:
        recent = "\n".join(f"[{c.author}]: {c.body}" for c in payload.recent_comments)
        sections += ["## Recent Comments", recent, ""]

    if total_suggestions > included_suggestions:
        sections += [
            f"*Note: {total_suggestions} inline suggestions total; "
            f"{included_suggestions} will be posted (top by severity). "
            "Mention the truncation briefly in the review body.*",
            "",
        ]

    sections.append("## Analysis Reports")
    for i, report in enumerate(reports, 1):
        if report.failed:
            sections.append(
                f"\n### Workstream {i}: {report.plan.focus} — FAILED\n"
                f"Error: {report.error or '(unknown)'}"
            )
        else:
            sections.append(
                f"\n### Workstream {i}: {report.plan.focus}  (severity: {report.severity})\n"
                + report.findings
            )

    return "\n".join(sections)


async def _post_error_and_cleanup(ctx: Any, wiki_root: Path, message: str) -> None:
    try:
        await ctx.commenter.post_comment(ctx.pr_number, f"**Argus error:** {message}")
    except Exception as exc:
        logger.warning("Failed to post Summary error comment: %s", exc)

    owner, repo_name = ctx.repo.split("/", 1)
    project = wiki_root / "argus" / owner / repo_name
    _delete_op_files(_scan_operation_files(project, ctx.pr_number))


# ---------------------------------------------------------------------------
# WikiMerge helpers
# ---------------------------------------------------------------------------

def _scan_operation_files(project: Path, pr_number: int) -> list[Path]:
    tmp_dir = project / "tmp"
    if not tmp_dir.exists():
        return []
    return [p for p in tmp_dir.glob(f"pr{pr_number}-*/**/*") if p.is_file()]


def _split_structure_ops(op_files: list[Path]) -> tuple[list[dict], list[Path]]:
    structure_ops: list[dict] = []
    memory_ops: list[Path] = []
    for path in op_files:
        if path.suffix != ".json":
            memory_ops.append(path)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("type") == "structure_upsert":
                structure_ops.append(data)
                continue
        except Exception:
            pass
        memory_ops.append(path)
    return structure_ops, memory_ops


def _apply_structure_upserts(project: Path, ops: list[dict], commit_sha: str) -> None:
    if not ops:
        return
    structure_path = project / _STRUCTURE_PATH
    current = structure_path.read_text(encoding="utf-8") if structure_path.exists() else ""
    sections = _parse_sections(current)

    modules = _parse_module_entries(sections.get("## Modules", ""))
    conflicts = 0
    for op in ops:
        module = str(op.get("module", "")).strip()
        description = str(op.get("description", "")).strip()
        if not module or not description:
            continue
        if module in modules:
            conflicts += 1
        modules[module] = description

    sections["## Modules"] = _render_module_entries(modules)
    sections["## Last Updated"] = _render_last_updated(commit_sha)
    structure_path.parent.mkdir(parents=True, exist_ok=True)
    structure_path.write_text(_render_sections(sections), encoding="utf-8")
    logger.info("WikiMerge structure_upsert applied ops=%d modules=%d conflicts=%d", len(ops), len(modules), conflicts)


def _parse_sections(content: str) -> dict[str, str]:
    if not content.strip():
        return {
            "## Modules": "",
            "## Runtime Flow": "",
            "## Conventions": "",
            "## Observed Paths (auto-generated, truncated)": "",
            "## Last Updated": "",
        }
    lines = content.splitlines()
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        if line.startswith("## "):
            current = line.strip()
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _parse_module_entries(modules_body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in modules_body.splitlines():
        line = line.strip()
        if not line.startswith("- `") or "`:" not in line:
            continue
        try:
            module = line.split("`", 2)[1]
            description = line.split("`:", 1)[1].strip()
            if module:
                out[module] = description
        except Exception:
            continue
    return out


def _render_module_entries(modules: dict[str, str]) -> str:
    if not modules:
        return "- `core`: TODO describe repository modules."
    return "\n".join(f"- `{k}`: {modules[k]}" for k in sorted(modules.keys()))


def _render_last_updated(commit_sha: str) -> str:
    from datetime import UTC, datetime

    ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return f"- commit_sha: `{commit_sha[:12]}`\n- merged_at: `{ts}`"


def _render_sections(sections: dict[str, str]) -> str:
    required = [
        "## Modules",
        "## Runtime Flow",
        "## Conventions",
        "## Observed Paths (auto-generated, truncated)",
        "## Last Updated",
    ]
    out = ["# Codebase Structure", ""]
    for header in required:
        out.append(header)
        out.append(sections.get(header, ""))
        out.append("")
    for header, body in sections.items():
        if header in required:
            continue
        out.append(header)
        out.append(body)
        out.append("")
    return "\n".join(out).strip() + "\n"


def _build_merge_message(
    pr_number: int,
    op_files: list[Path],
    existing_pages: dict[str, str],
) -> str:
    ops_by_target: dict[str, list[tuple[str, str]]] = {}
    for f in op_files:
        try:
            raw = f.read_text(encoding="utf-8")
            target, operation, content = _parse_op_file(raw)
        except Exception:
            continue
        ops_by_target.setdefault(target, []).append((operation, content))

    sections = [
        f"## Staged Wiki Operations for PR #{pr_number}",
        f"There are {len(op_files)} staged operation(s) across "
        f"{len(ops_by_target)} target page(s).",
        "",
        "Existing page content is pre-loaded below. Merge each target and call",
        "memory_write or memory_append. Use memory_read only for other wiki pages.",
        "",
    ]

    for target, ops in ops_by_target.items():
        sections.append(f"### Target: {target}")
        if target in existing_pages:
            sections.append("\n**Existing content:**")
            sections.append(existing_pages[target])
        else:
            sections.append("\n*(page does not exist yet)*")
        for i, (operation, content) in enumerate(ops, 1):
            sections.append(f"\n**Staged {operation} #{i}:**")
            sections.append(content)
        sections.append("")

    return "\n".join(sections)


def _parse_op_file(raw: str) -> tuple[str, str, str]:
    """Parse operation file. Returns (target, operation, content)."""
    if not raw.startswith("---"):
        return "unknown.md", "append", raw.strip()

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return "unknown.md", "append", raw.strip()

    frontmatter, body = parts[1], parts[2].strip()
    target = "unknown.md"
    operation = "append"
    for line in frontmatter.splitlines():
        if line.startswith("target:"):
            target = line.split(":", 1)[1].strip()
        elif line.startswith("operation:"):
            operation = line.split(":", 1)[1].strip()

    return target, operation, body


def _delete_op_files(op_files: list[Path]) -> None:
    for f in op_files:
        try:
            f.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to delete tmp file %s: %s", f, exc)
    for d in {f.parent for f in op_files}:
        try:
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared ReAct dispatch helper
# ---------------------------------------------------------------------------

async def _dispatch_tool(
    tool_index: dict[str, BoundTool], call: ToolCall
) -> ToolResult:
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
