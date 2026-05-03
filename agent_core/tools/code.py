"""Repository navigation tools.

All tools access reader/identifiers via get_session() at call time — they are
constructed once per pipeline run, bound to the pre-loaded file tree and config
limits. No tool raises; all errors are returned as descriptive strings.
"""

from __future__ import annotations

import fnmatch

from llm_framework import BoundTool

from ..context import get_session
from ..domain import FileTree


class _ListTool(BoundTool):
    def __init__(self, file_tree: FileTree | None) -> None:
        super().__init__(
            name="list",
            description=(
                "List all file paths under a directory (relative to repo root). "
                "Uses the pre-loaded file tree — zero API calls. "
                "Returns one path per line. Returns empty string if path doesn't exist."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to repo root (e.g. 'src/auth').",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Optional glob filter on filenames (e.g. '*.py').",
                    },
                },
                "required": ["path"],
            },
        )
        self._file_tree = file_tree

    async def process(self, arguments: dict) -> str:
        if self._file_tree is None:
            return "Error: file tree unavailable for this run (TreeLoad failed)."

        path = arguments["path"].rstrip("/")
        pattern = arguments.get("pattern")

        prefix = path + "/"
        matches = [
            p for p in self._file_tree.paths
            if p.startswith(prefix) or p == path
        ]

        if pattern:
            matches = [p for p in matches if fnmatch.fnmatch(p.split("/")[-1], pattern)]

        return "\n".join(matches)


class _ReadDiffTool(BoundTool):
    def __init__(self) -> None:
        super().__init__(
            name="read_diff",
            description="Return the unified diff patch for one changed file in this PR.",
            schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to repo root.",
                    },
                },
                "required": ["path"],
            },
        )

    async def process(self, arguments: dict) -> str:
        ctx = get_session()
        path = arguments["path"]
        try:
            return await ctx.reader.fetch_diff(ctx.pr_number, path)
        except Exception as exc:
            return f"Error reading diff for {path!r}: {exc}"


class _ReadFileTool(BoundTool):
    def __init__(self, max_window_lines: int) -> None:
        super().__init__(
            name="read_file",
            description=(
                "Read a range of lines from a file at the current PR commit. "
                "Output is line-numbered: '42: def foo():'. "
                f"Window capped at {max_window_lines} lines — use smaller ranges for large files."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to repo root.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to read (1-based).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to read (inclusive, 1-based).",
                    },
                },
                "required": ["path", "start_line", "end_line"],
            },
        )
        self._max_window = max_window_lines

    async def process(self, arguments: dict) -> str:
        ctx = get_session()
        path = arguments["path"]
        start = max(1, int(arguments["start_line"]))
        end = int(arguments["end_line"])

        truncated = False
        if end - start + 1 > self._max_window:
            end = start + self._max_window - 1
            truncated = True

        content: str | None = None
        last_exc: Exception | None = None
        for _ in range(2):
            try:
                content = await ctx.reader.fetch_file(path, ctx.commit_sha)
                break
            except Exception as exc:
                last_exc = exc

        if content is None:
            return f"Error reading {path!r}: {last_exc}"

        lines = content.splitlines()
        total = len(lines)

        if start > total:
            return (
                f"Error: {path!r} has {total} lines; "
                f"start_line={start} is out of bounds."
            )

        clamped_end = min(end, total)
        slice_ = lines[start - 1 : clamped_end]
        output = "\n".join(f"{start + i}: {line}" for i, line in enumerate(slice_))

        if truncated:
            output += (
                f"\n[window capped at {self._max_window} lines; "
                "request a smaller range to read more]"
            )
        elif clamped_end < end:
            output += f"\n[note: file has {total} lines; end_line clamped to {total}]"

        return output


class _SearchTool(BoundTool):
    def __init__(self, file_tree: FileTree | None, max_results: int) -> None:
        super().__init__(
            name="search",
            description=(
                "Substring search across the repository (or a scoped path). "
                "Returns grep-style output: 'path/file.py:42: matching line'. "
                f"Bounded at {max_results} results."
            ),
            schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Substring to search for (not a regex).",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional path prefix to scope the search.",
                    },
                },
                "required": ["pattern"],
            },
        )
        self._file_tree = file_tree
        self._max_results = max_results

    async def process(self, arguments: dict) -> str:
        if self._file_tree is None:
            return "Error: file tree unavailable for this run (TreeLoad failed)."

        ctx = get_session()
        pattern = arguments["pattern"]
        scope = arguments.get("path", "").rstrip("/")

        candidates = self._file_tree.paths
        if scope:
            candidates = [
                p for p in candidates
                if p.startswith(scope + "/") or p == scope
            ]

        results: list[str] = []
        for file_path in candidates:
            if len(results) >= self._max_results:
                break
            try:
                content = await ctx.reader.fetch_file(file_path, ctx.commit_sha)
            except Exception:
                continue
            for lineno, line in enumerate(content.splitlines(), start=1):
                if pattern in line:
                    results.append(f"{file_path}:{lineno}: {line}")
                    if len(results) >= self._max_results:
                        break

        if not results:
            return f"No matches for {pattern!r}."

        output = "\n".join(results)
        if len(results) >= self._max_results:
            output += f"\n[truncated: showing first {self._max_results} matches]"
        return output


def make_code_tools(
    file_tree: FileTree | None,
    max_file_window_lines: int,
    max_search_results: int,
) -> list[BoundTool]:
    """Return the four repository navigation tools bound to the given constraints."""
    return [
        _ListTool(file_tree),
        _ReadDiffTool(),
        _ReadFileTool(max_file_window_lines),
        _SearchTool(file_tree, max_search_results),
    ]
