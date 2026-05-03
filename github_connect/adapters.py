from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from agent_core.domain import ChangedFile, Comment, Commit, InlineComment, PRMetadata
from agent_core.ports import CodeReader, Commenter

from .github_api import GitHubApiClient


@dataclass
class ReaderRunState:
    warnings: list[str]


class GitHubCodeReader(CodeReader):
    def __init__(
        self,
        api: GitHubApiClient,
        installation_id: int,
        repo: str,
        page_cap: int = 20,
        file_cap: int = 300,
    ) -> None:
        self._api = api
        self._installation_id = installation_id
        self._repo = repo
        self._page_cap = page_cap
        self._file_cap = file_cap
        self._file_cache: dict[tuple[str, str], str] = {}
        self._tree_cache: dict[str, list[str]] = {}
        self._pr_files_page_cache: dict[tuple[int, int], list[dict]] = {}
        self._state = ReaderRunState(warnings=[])

    async def fetch_pr_metadata(self, pr_number: int) -> PRMetadata:
        data = await self._api.get_json(self._installation_id, f"/repos/{self._repo}/pulls/{pr_number}")
        description = data.get("body") or ""
        if self._state.warnings:
            description += "\n\n[ARGUS_CONTEXT] " + " | ".join(self._state.warnings)
        return PRMetadata(
            title=data["title"],
            description=description,
            author=data["user"]["login"],
            base_branch=data["base"]["ref"],
            head_branch=data["head"]["ref"],
            labels=[l["name"] for l in data.get("labels", [])],
            pr_number=data["number"],
            draft=bool(data.get("draft", False)),
        )

    async def fetch_commits(self, pr_number: int) -> list[Commit]:
        out: list[Commit] = []
        page = 1
        while page <= self._page_cap:
            rows = await self._api.get_json(
                self._installation_id,
                f"/repos/{self._repo}/pulls/{pr_number}/commits",
                params={"per_page": 100, "page": page},
            )
            if not rows:
                break
            for c in rows:
                out.append(Commit(sha=c["sha"], message=c["commit"]["message"]))
            page += 1
        if self._state.warnings:
            out.insert(0, Commit(sha="0000000", message="[ARGUS_CONTEXT] " + " | ".join(self._state.warnings)))
        return out

    async def fetch_changed_files(self, pr_number: int) -> list[ChangedFile]:
        out: list[ChangedFile] = []
        page = 1
        while page <= self._page_cap and len(out) < self._file_cap:
            rows = await self._api.get_json(
                self._installation_id,
                f"/repos/{self._repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            if not rows:
                break
            for f in rows:
                if len(out) >= self._file_cap:
                    break
                status = f["status"]
                if status not in {"added", "modified", "deleted"}:
                    status = "modified"
                out.append(
                    ChangedFile(
                        path=f["filename"],
                        status=status,  # type: ignore[arg-type]
                        additions=int(f.get("additions", 0)),
                        deletions=int(f.get("deletions", 0)),
                    )
                )
            page += 1
        if len(out) >= self._file_cap:
            self._state.warnings.append(
                f"Changed files truncated at {self._file_cap}; review coverage may be partial."
            )
        return out

    async def fetch_changed_files_since(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        data = await self._api.get_json(
            self._installation_id,
            f"/repos/{self._repo}/compare/{base_sha}...{head_sha}",
            params={"per_page": 100, "page": 1},
        )
        rows = data.get("files", []) if isinstance(data, dict) else []
        out: list[ChangedFile] = []
        for f in rows[: self._file_cap]:
            status = f.get("status", "modified")
            if status not in {"added", "modified", "deleted"}:
                status = "modified"
            out.append(
                ChangedFile(
                    path=f["filename"],
                    status=status,  # type: ignore[arg-type]
                    additions=int(f.get("additions", 0)),
                    deletions=int(f.get("deletions", 0)),
                )
            )
        if len(rows) >= self._file_cap:
            self._state.warnings.append(
                f"Sync delta files truncated at {self._file_cap}; incremental review coverage may be partial."
            )
        return out

    async def fetch_diff(self, pr_number: int, path: str) -> str:
        page = 1
        while page <= self._page_cap:
            cache_key = (pr_number, page)
            rows = self._pr_files_page_cache.get(cache_key)
            if rows is None:
                rows = await self._api.get_json(
                    self._installation_id,
                    f"/repos/{self._repo}/pulls/{pr_number}/files",
                    params={"per_page": 100, "page": page},
                )
                self._pr_files_page_cache[cache_key] = rows

            if not rows:
                break

            for row in rows:
                if row.get("filename") == path:
                    return row.get("patch", "")

            page += 1
        return ""

    async def fetch_file(self, path: str, ref: str) -> str:
        cache_key = (path, ref)
        if cache_key in self._file_cache:
            return self._file_cache[cache_key]
        data = await self._api.get_json(
            self._installation_id,
            f"/repos/{self._repo}/contents/{path}",
            params={"ref": ref},
        )
        import base64
        content = base64.b64decode(data.get("content", "")).decode("utf-8")
        self._file_cache[cache_key] = content
        return content

    async def fetch_tree(self, ref: str) -> list[str]:
        if ref in self._tree_cache:
            return self._tree_cache[ref]
        data = await self._api.get_json(
            self._installation_id,
            f"/repos/{self._repo}/git/trees/{ref}",
            params={"recursive": 1},
        )
        paths = [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]
        self._tree_cache[ref] = paths
        return paths

    async def fetch_comments(self, pr_number: int, after: str | None = None) -> list[Comment]:
        issue_comments = await self._get_paginated(
            f"/repos/{self._repo}/issues/{pr_number}/comments"
        )
        review_comments = await self._get_paginated(
            f"/repos/{self._repo}/pulls/{pr_number}/comments"
        )
        reviews = await self._get_paginated(
            f"/repos/{self._repo}/pulls/{pr_number}/reviews"
        )
        merged = [
            Comment(
                id=str(c["id"]),
                author=c["user"]["login"],
                body=c.get("body", ""),
                created_at=c["created_at"],
                is_inline=False,
                position=None,
            )
            for c in issue_comments
        ] + [
            Comment(
                id=str(c["id"]),
                author=c["user"]["login"],
                body=c.get("body", ""),
                created_at=c["created_at"],
                is_inline=True,
                position=c.get("position"),
            )
            for c in review_comments
        ] + [
            Comment(
                id=str(c["id"]),
                author=c["user"]["login"],
                body=c.get("body", ""),
                created_at=c.get("submitted_at") or c.get("created_at", ""),
                is_inline=False,
                position=None,
            )
            for c in reviews
            if c.get("submitted_at")
        ]
        merged.sort(key=lambda c: c.created_at)
        if not after:
            return merged
        idx = next((i for i, c in enumerate(merged) if c.id == after), -1)
        if idx == -1:
            return merged
        return merged[idx + 1 :]

    async def _get_paginated(self, path: str) -> list[dict]:
        out: list[dict] = []
        per_page = 100
        page = 1
        while page <= self._page_cap:
            rows = await self._api.get_json(
                self._installation_id,
                path,
                params={"per_page": per_page, "page": page},
            )
            if not rows:
                break
            if isinstance(rows, list):
                out.extend(rows)
                if len(rows) < per_page:
                    break
            else:
                break
            page += 1
        return out


class GitHubCommenter(Commenter):
    def __init__(self, api: GitHubApiClient, installation_id: int, repo: str) -> None:
        self._api = api
        self._installation_id = installation_id
        self._repo = repo

    async def post_comment(self, pr_number: int, body: str) -> str:
        data = await self._api.post_json(
            self._installation_id,
            f"/repos/{self._repo}/issues/{pr_number}/comments",
            json={"body": body},
        )
        return str(data.get("id", ""))

    async def post_review(
        self,
        pr_number: int,
        body: str,
        verdict: Literal["approve", "request_changes", "comment"],
        inline_comments: list[InlineComment],
    ) -> None:
        event = {
            "approve": "APPROVE",
            "request_changes": "REQUEST_CHANGES",
            "comment": "COMMENT",
        }[verdict]
        comments = [_to_review_comment(c) for c in inline_comments]
        payload = {"body": body, "event": event, "comments": comments}
        try:
            await self._api.post_json(
                self._installation_id,
                f"/repos/{self._repo}/pulls/{pr_number}/reviews",
                json=payload,
            )
        except Exception:
            note = "\n\n[Argus note] Some inline suggestions could not be posted and were omitted."
            await self._api.post_json(
                self._installation_id,
                f"/repos/{self._repo}/pulls/{pr_number}/reviews",
                json={"body": body + note, "event": event},
            )


def _to_review_comment(c: InlineComment) -> dict:
    if c.start_line == c.end_line:
        return {"path": c.path, "body": c.body, "line": c.end_line, "side": "RIGHT"}
    return {
        "path": c.path,
        "body": c.body,
        "start_line": c.start_line,
        "start_side": "RIGHT",
        "line": c.end_line,
        "side": "RIGHT",
    }
