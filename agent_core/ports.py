"""Port interfaces. Defined here, implemented by github_connect."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from .domain import ChangedFile, Comment, Commit, InlineComment, PRMetadata


class CodeReader(ABC):
    """Read-only access to repository content and PR discussion."""

    @abstractmethod
    async def fetch_pr_metadata(self, pr_number: int) -> PRMetadata: ...

    @abstractmethod
    async def fetch_commits(self, pr_number: int) -> list[Commit]: ...

    @abstractmethod
    async def fetch_changed_files(self, pr_number: int) -> list[ChangedFile]: ...

    @abstractmethod
    async def fetch_changed_files_since(
        self,
        base_sha: str,
        head_sha: str,
    ) -> list[ChangedFile]: ...

    @abstractmethod
    async def fetch_diff(self, pr_number: int, path: str) -> str: ...

    @abstractmethod
    async def fetch_file(self, path: str, ref: str) -> str: ...

    @abstractmethod
    async def fetch_tree(self, ref: str) -> list[str]: ...  # flat blob paths only

    @abstractmethod
    async def fetch_comments(
        self, pr_number: int, after: str | None = None
    ) -> list[Comment]: ...


class Commenter(ABC):
    """Write access to PR comments and reviews."""

    @abstractmethod
    async def post_comment(self, pr_number: int, body: str) -> str: ...  # returns comment_id

    @abstractmethod
    async def post_review(
        self,
        pr_number: int,
        body: str,
        verdict: Literal["approve", "request_changes", "comment"],
        inline_comments: list[InlineComment],
    ) -> None: ...
