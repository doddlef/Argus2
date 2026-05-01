"""Loading-phase nodes.

All nodes are pure data loaders — no LLM calls except ThreadLoad's optional
compaction step (fast client). Each receives PipelineState[None] and returns
PipelineState[None] after mutating payload fields in place.

Failure policy:
  PRLoad    — re-raises; pipeline aborts (no review posted)
  Acknowledge — logs and continues (non-critical)
  MemLoad   — continues with wiki_index/wiki_structure = ""
  TreeLoad  — continues degraded (file_tree = None; list/search tools return errors)
  ThreadLoad — continues with thread_summary=None, recent_comments=[]
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from llm_framework import LLMClient, Message
from llm_framework.workflow import Node

from ..context import PipelinePayload, PipelineState, get_session
from ..domain import Comment, FileTree

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PRLoad
# ---------------------------------------------------------------------------

class PRLoadNode(Node):
    """Fetches commits and changed files; creates the initial PipelineState."""

    async def execute(self, _state: Any) -> PipelineState[None]:
        ctx = get_session()
        commits = await ctx.reader.fetch_commits(ctx.pr_number)
        changed_files = await ctx.reader.fetch_changed_files(ctx.pr_number)
        return PipelineState(
            payload=PipelinePayload(commits=commits, changed_files=changed_files),
            data=None,
        )


# ---------------------------------------------------------------------------
# Acknowledge
# ---------------------------------------------------------------------------

class AcknowledgeNode(Node):
    """Posts a short acknowledgment comment so the author knows Argus is working.

    Constructed with the appropriate message template by the pipeline builder.
    When enabled=False the node is a transparent pass-through.
    """

    def __init__(self, message: str, enabled: bool = True) -> None:
        self._message = message
        self._enabled = enabled

    async def execute(self, state: PipelineState[None]) -> PipelineState[None]:
        if not self._enabled:
            return state
        ctx = get_session()
        try:
            await ctx.commenter.post_comment(ctx.pr_number, self._message)
        except Exception as exc:
            logger.warning("Acknowledge failed (non-critical): %s", exc)
        return state


# ---------------------------------------------------------------------------
# MemLoad
# ---------------------------------------------------------------------------

class MemLoadNode(Node):
    """Reads index.md and structure.md from the wiki; populates payload wiki fields."""

    def __init__(self, wiki_root: Path) -> None:
        self._wiki_root = wiki_root

    async def execute(self, state: PipelineState[None]) -> PipelineState[None]:
        ctx = get_session()
        owner, repo_name = ctx.repo.split("/", 1)
        project = self._wiki_root / "argus" / owner / repo_name

        state.payload.wiki_index = _read_optional(project / "index.md")
        state.payload.wiki_structure = _read_optional(project / "structure.md")
        return state


def _read_optional(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# TreeLoad
# ---------------------------------------------------------------------------

class TreeLoadNode(Node):
    """Fetches the full file tree, simplifies it, and stores it in payload."""

    async def execute(self, state: PipelineState[None]) -> PipelineState[None]:
        ctx = get_session()
        try:
            paths = await ctx.reader.fetch_tree(ctx.commit_sha)
            state.payload.file_tree = _build_file_tree(paths)
        except Exception as exc:
            logger.warning("TreeLoad failed — list/search degraded for this run: %s", exc)
        return state


def _build_file_tree(paths: list[str]) -> FileTree:
    trie = _build_trie(paths)
    simplified = _simplify_trie(trie)
    lines: list[str] = []
    _render_trie(simplified, depth=0, lines=lines)
    return FileTree(paths=paths, rendered="\n".join(lines))


def _build_trie(paths: list[str]) -> dict:
    trie: dict = {}
    for path in paths:
        parts = path.split("/")
        node = trie
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = True  # file leaf
    return trie


def _simplify_trie(node: dict) -> dict:
    """Collapse single-child directory chains bottom-up."""
    result = {}
    for key, child in node.items():
        if not isinstance(child, dict):  # file leaf
            result[key] = child
            continue
        simplified = _simplify_trie(child)
        full_key = key
        while len(simplified) == 1:
            only_key, only_val = next(iter(simplified.items()))
            if not isinstance(only_val, dict):
                break  # only child is a file — stop collapsing
            full_key = f"{full_key}/{only_key}"
            simplified = _simplify_trie(only_val)
        result[full_key] = simplified
    return result


def _render_trie(node: dict, depth: int, lines: list[str]) -> None:
    for key in sorted(node.keys()):
        child = node[key]
        indent = "  " * depth
        if not isinstance(child, dict):
            lines.append(f"{indent}{key}")
        elif depth >= 4:
            count = _count_files(child)
            lines.append(f"{indent}{key}/ ({count} files)")
        else:
            lines.append(f"{indent}{key}/")
            _render_trie(child, depth + 1, lines)


def _count_files(node: dict) -> int:
    return sum(
        _count_files(v) if isinstance(v, dict) else 1
        for v in node.values()
    )


# ---------------------------------------------------------------------------
# ThreadLoad
# ---------------------------------------------------------------------------

class ThreadLoadNode(Node):
    """Loads the PR thread; runs compaction when new comment count exceeds threshold.

    bot_username is used to filter out Argus's own acknowledgment comments before
    compaction (reduces token waste).
    """

    def __init__(
        self,
        wiki_root: Path,
        fast_client: LLMClient,
        threshold: int,
        bot_username: str,
    ) -> None:
        self._wiki_root = wiki_root
        self._fast_client = fast_client
        self._threshold = threshold
        self._bot_username = bot_username

    async def execute(self, state: PipelineState[None]) -> PipelineState[None]:
        ctx = get_session()
        thread_file = self._thread_file(ctx.repo, ctx.pr_number)

        last_comment_id: str | None = None
        existing_summary: str = ""
        if thread_file.exists():
            last_comment_id, existing_summary = _parse_compaction_file(
                thread_file.read_text(encoding="utf-8")
            )

        try:
            new_comments = await ctx.reader.fetch_comments(
                ctx.pr_number, after=last_comment_id
            )
        except Exception as exc:
            logger.warning("ThreadLoad failed — no thread context: %s", exc)
            return state

        # Filter Argus's own acknowledgment comments before compaction
        non_bot = [c for c in new_comments if c.author != self._bot_username]

        if len(non_bot) > self._threshold:
            try:
                summary, new_last_id = await _compact(
                    existing_summary, non_bot, self._fast_client
                )
                thread_file.parent.mkdir(parents=True, exist_ok=True)
                thread_file.write_text(
                    _write_compaction_file(new_last_id, ctx.commit_sha, summary),
                    encoding="utf-8",
                )
                state.payload.thread_summary = summary
                state.payload.recent_comments = []
                return state
            except Exception as exc:
                logger.warning("Thread compaction failed — using raw comments: %s", exc)

        state.payload.thread_summary = existing_summary or None
        state.payload.recent_comments = non_bot
        return state

    def _thread_file(self, repo: str, pr_number: int) -> Path:
        owner, repo_name = repo.split("/", 1)
        return (
            self._wiki_root
            / "argus" / owner / repo_name
            / "pr" / "threads"
            / f"{pr_number}.md"
        )


# ---------------------------------------------------------------------------
# Thread compaction helpers
# ---------------------------------------------------------------------------

async def _compact(
    existing_summary: str,
    comments: list[Comment],
    client: LLMClient,
) -> tuple[str, str]:
    """Compact thread comments into a summary. Returns (summary, last_comment_id)."""
    thread_text = "\n\n".join(
        f"[{c.id}] {c.author}: {c.body}" for c in comments
    )
    prior = f"Prior summary:\n{existing_summary}\n\n" if existing_summary else ""

    response = await client.complete(
        messages=[
            Message.text(
                "user",
                f"{prior}Comments to compact:\n\n{thread_text}\n\n"
                "Write a concise summary covering: key decisions made, unresolved questions, "
                "Argus's last review verdict and the commit SHA it reviewed. "
                "Exclude resolved nitpicks.",
            )
        ],
        tools=None,
        system=(
            "You are compacting a PR review thread into a concise summary for future context. "
            "Output only the summary text — no preamble or meta-commentary."
        ),
    )

    summary = response.content or ""
    last_id = comments[-1].id
    return summary, last_id


def _parse_compaction_file(content: str) -> tuple[str | None, str]:
    """Parse a compaction file. Returns (last_comment_id, summary_text)."""
    if not content.startswith("---"):
        return None, content.strip()

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content.strip()

    frontmatter, body = parts[1], parts[2].strip()

    last_comment_id: str | None = None
    for line in frontmatter.splitlines():
        if line.startswith("last_comment_id:"):
            last_comment_id = line.split(":", 1)[1].strip().strip('"')

    return last_comment_id, body


def _write_compaction_file(last_comment_id: str, sha: str, summary: str) -> str:
    return (
        f'---\nlast_comment_id: "{last_comment_id}"\n'
        f"compacted_at_sha: {sha}\n---\n\n{summary}\n"
    )
