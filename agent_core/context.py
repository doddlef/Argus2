"""Pipeline context types — all three layers.

  PipelinePayload / PipelineState[T]  node-to-node state
  SessionContext                       immutable per-run identifiers + ports
  ApplicationContext / ClientTier      global singleton constructed at startup
  PRLockManager                        per-PR asyncio.Lock
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Generic, TypeVar

from llm_framework import LLMClient

from .config import ArgusConfig
from .domain import ChangedFile, Comment, Commit, FileTree, PRMetadata
from .ports import CodeReader, Commenter

T = TypeVar("T")

# ---------------------------------------------------------------------------
# SessionContext ContextVar — one per asyncio task via create_task() copy
# ---------------------------------------------------------------------------

_session: ContextVar[SessionContext] = ContextVar("session")


@asynccontextmanager
async def session(ctx: SessionContext) -> AsyncIterator[SessionContext]:
    token = _session.set(ctx)
    try:
        yield ctx
    finally:
        _session.reset(token)


def get_session() -> SessionContext:
    """Return the current SessionContext. Raises LookupError if not set — intentional fast-fail."""
    return _session.get()


@dataclass
class PipelinePayload:
    """Mutable accumulator for loading-phase data.

    Loading nodes mutate fields in place (loading is sequential, so no write-safety
    concern). FanOutEdge copies the reference into each branch; Analysis nodes only
    read payload, never mutate it after fan-out.
    """

    commits: list[Commit] = field(default_factory=list)
    changed_files: list[ChangedFile] = field(default_factory=list)
    wiki_index: str = ""
    wiki_structure: str = ""
    file_tree: FileTree | None = None
    thread_summary: str | None = None
    recent_comments: list[Comment] = field(default_factory=list)


@dataclass
class PipelineState(Generic[T]):
    """Generic state wrapper passed between nodes and edges.

    payload: accumulated loading data; mutated in place during loading phase.
    data:    node-specific output; typed via T.
    fan_n:   total branches in the current fan-out section; None outside it.
    fan_i:   this branch's 0-based index; None outside fan sections.
    """

    payload: PipelinePayload
    data: T
    fan_n: int | None = None
    fan_i: int | None = None


# ---------------------------------------------------------------------------
# SessionContext — immutable per-run identifiers and ports
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionContext:
    installation_id: int
    repo: str           # "owner/repo"
    pr_number: int
    commit_sha: str
    actor: str          # GitHub user who triggered the event
    reader: CodeReader
    commenter: Commenter
    metadata: PRMetadata  # fetched by Dispatcher before pipeline starts


# ---------------------------------------------------------------------------
# ApplicationContext — global singleton, constructed once at startup
# ---------------------------------------------------------------------------

class PRLockManager:
    """Provides a per-PR asyncio.Lock keyed by (installation_id, pr_number).

    Usage: async with app_ctx.pr_lock(installation_id, pr_number): ...
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    def __call__(self, installation_id: int, pr_number: int) -> asyncio.Lock:
        key = (installation_id, pr_number)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]


@dataclass(frozen=True)
class ClientTier:
    fast: LLMClient      # triage, route, acknowledge
    standard: LLMClient  # conversation, summary, wiki_merge
    deep: LLMClient      # analysis (overridable per Plan)


@dataclass(frozen=True)
class ApplicationContext:
    clients: ClientTier
    wiki_root: Path           # convenience shortcut; mirrors config.wiki_root
    pr_lock: PRLockManager
    bot_username: str         # convenience shortcut; mirrors config.bot_username
    config: ArgusConfig
