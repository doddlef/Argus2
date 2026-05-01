"""Pure domain dataclasses. No logic, no external dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class PRMetadata:
    title: str
    description: str
    author: str
    base_branch: str
    head_branch: str
    labels: list[str]
    pr_number: int
    draft: bool


@dataclass
class Commit:
    sha: str
    message: str


@dataclass
class ChangedFile:
    path: str
    status: Literal["added", "modified", "deleted"]
    additions: int
    deletions: int


@dataclass
class Comment:
    id: str
    author: str
    body: str
    created_at: str
    is_inline: bool
    position: int | None  # diff position for inline comments; None for top-level


@dataclass
class FileTree:
    paths: list[str]   # all blob paths relative to repo root
    rendered: str       # pre-simplified, depth-capped tree for prompts


@dataclass
class InlineComment:
    """Passed to Commenter.post_review(). Severity is stripped before this point."""

    path: str
    start_line: int
    end_line: int
    body: str


@dataclass
class InlineSuggestion:
    """Produced by Analysis agents. Converted to InlineComment for the commenter port."""

    path: str
    start_line: int
    end_line: int       # equal to start_line for single-line comments
    body: str
    severity: Literal["critical", "high", "medium", "low"]


@dataclass
class Plan:
    """One analysis workstream produced by Triage."""

    files: list[str]                              # assigned files; no overlap across plans
    focus: str                                    # free-text label, e.g. "auth security"
    guidance: str                                 # directive prompt for the Analysis agent
    tier: Literal["fast", "standard", "deep"]
    preload_diffs: bool  # True when total diff lines for these files < preload_diff_threshold


@dataclass
class AnalysisReport:
    """Produced by each Analysis agent. Always returned; failed runs set failed=True."""

    plan: Plan
    findings: str
    inline_suggestions: list[InlineSuggestion] = field(default_factory=list)
    severity: Literal["critical", "high", "medium", "low", "none"] = "none"
    failed: bool = False
    error: str = ""


RouteDecision = Literal["conversation", "analysis"]
