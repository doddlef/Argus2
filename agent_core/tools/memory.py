"""Wiki memory tools.

Two variants:
  make_memory_tools()          — direct canonical writes (Conversation, WikiMerge)
  make_analysis_memory_tools() — memory_write/append redirected to tmp/ operation files
                                 (Analysis nodes); memory_read is unchanged

Operation file format (tmp/pr{n}-{run_id}/{path}):
  ---
  target:      module-auth.md
  operation:   write | append
  description: one-line index entry
  run_id:      <uuid>
  ---

  {content from the agent's call}

WikiMerge scans tmp/ recursively for matching files and applies them to canonical paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

from llm_framework import BoundTool

from ..context import get_session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_root(wiki_root: Path) -> Path:
    ctx = get_session()
    owner, repo_name = ctx.repo.split("/", 1)
    return wiki_root / "argus" / owner / repo_name


def _resolve_safe_wiki_path(project: Path, raw_path: str) -> Tuple[Path | None, str | None]:
    """Resolve a wiki page path and ensure it stays within project root."""
    path = Path(raw_path)
    if path.is_absolute():
        return None, "Error: path must be relative to the project wiki root."

    candidate = (project / path).resolve(strict=False)
    project_resolved = project.resolve(strict=False)
    try:
        candidate.relative_to(project_resolved)
    except ValueError:
        return None, "Error: path escapes project wiki root."

    return candidate, None


def _update_index(project: Path, path: str, description: str) -> None:
    """Atomically replace or append the index.md entry for path."""
    index_path = project / "index.md"
    entry = f"- {path} — {description}"

    if not index_path.exists():
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(entry + "\n", encoding="utf-8")
        return

    lines = index_path.read_text(encoding="utf-8").splitlines()
    prefix = f"- {path} "
    replaced = False
    new_lines = []
    for line in lines:
        if line.startswith(prefix):
            new_lines.append(entry)
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(entry)
    index_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Direct (canonical) tools
# ---------------------------------------------------------------------------

_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Wiki page path relative to the project wiki root (e.g. 'module-auth.md').",
        },
    },
    "required": ["path"],
}

_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Wiki page path to create or overwrite."},
        "content": {"type": "string", "description": "Full page content (Markdown)."},
        "description": {
            "type": "string",
            "description": "One-line index entry describing this page (≤150 characters).",
        },
    },
    "required": ["path", "content", "description"],
}

_APPEND_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Wiki page path to append to (created if absent)."},
        "content": {"type": "string", "description": "Markdown content to append."},
        "description": {
            "type": "string",
            "description": "Updated one-line index entry for this page.",
        },
    },
    "required": ["path", "content", "description"],
}


class _MemReadTool(BoundTool):
    def __init__(self, wiki_root: Path) -> None:
        super().__init__(
            name="memory_read",
            description="Read a wiki page. Returns an error string if the page does not exist.",
            schema=_READ_SCHEMA,
        )
        self._wiki_root = wiki_root

    async def process(self, arguments: dict) -> str:
        project = _project_root(self._wiki_root)
        page_path, err = _resolve_safe_wiki_path(project, arguments["path"])
        if err:
            return err
        assert page_path is not None
        if not page_path.exists():
            return f"Error: wiki page {arguments['path']!r} does not exist."
        return page_path.read_text(encoding="utf-8")


class _MemWriteTool(BoundTool):
    def __init__(self, wiki_root: Path) -> None:
        super().__init__(
            name="memory_write",
            description=(
                "Create or overwrite a wiki page and update its index.md entry. "
                "Only write what a future reviewer could not re-derive from the current code."
            ),
            schema=_WRITE_SCHEMA,
        )
        self._wiki_root = wiki_root

    async def process(self, arguments: dict) -> str:
        path = arguments["path"]
        content = arguments["content"]
        description = arguments["description"]

        project = _project_root(self._wiki_root)
        page_path, err = _resolve_safe_wiki_path(project, path)
        if err:
            return err
        assert page_path is not None
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(content, encoding="utf-8")
        _update_index(project, path, description)
        return f"Written {path}."


class _MemAppendTool(BoundTool):
    def __init__(self, wiki_root: Path) -> None:
        super().__init__(
            name="memory_append",
            description=(
                "Append content to a wiki page (creates it if absent) and update its index entry. "
                "Only write what a future reviewer could not re-derive from the current code."
            ),
            schema=_APPEND_SCHEMA,
        )
        self._wiki_root = wiki_root

    async def process(self, arguments: dict) -> str:
        path = arguments["path"]
        content = arguments["content"]
        description = arguments["description"]

        project = _project_root(self._wiki_root)
        page_path, err = _resolve_safe_wiki_path(project, path)
        if err:
            return err
        assert page_path is not None
        page_path.parent.mkdir(parents=True, exist_ok=True)
        existing = page_path.read_text(encoding="utf-8") if page_path.exists() else ""
        page_path.write_text(existing + ("\n" if existing else "") + content, encoding="utf-8")
        _update_index(project, path, description)
        return f"Appended to {path}."


# ---------------------------------------------------------------------------
# Operation-file wrapped tools (Analysis nodes)
# ---------------------------------------------------------------------------

class _WrappedMemWriteTool(BoundTool):
    """Redirects memory_write to a tmp/ operation file instead of the canonical path."""

    def __init__(self, wiki_root: Path, pr_number: int, run_id: str) -> None:
        super().__init__(
            name="memory_write",
            description=(
                "Stage a wiki page write. The change will be merged into the wiki after "
                "all analysis workstreams complete. "
                "Only write what a future reviewer could not re-derive from the current code."
            ),
            schema=_WRITE_SCHEMA,
        )
        self._wiki_root = wiki_root
        self._pr_number = pr_number
        self._run_id = run_id

    async def process(self, arguments: dict) -> str:
        path = arguments["path"]
        content = arguments["content"]
        description = arguments["description"]

        project = _project_root(self._wiki_root)
        _validated, err = _resolve_safe_wiki_path(project, path)
        if err:
            return err
        tmp_path = project / "tmp" / f"pr{self._pr_number}-{self._run_id}" / path
        tmp_path.parent.mkdir(parents=True, exist_ok=True)

        frontmatter = (
            f"---\n"
            f"target: {path}\n"
            f"operation: write\n"
            f"description: {description}\n"
            f"run_id: {self._run_id}\n"
            f"---\n\n"
        )
        tmp_path.write_text(frontmatter + content, encoding="utf-8")
        return f"Staged write to {path}."


class _WrappedMemAppendTool(BoundTool):
    """Redirects memory_append to a tmp/ operation file instead of the canonical path."""

    def __init__(self, wiki_root: Path, pr_number: int, run_id: str) -> None:
        super().__init__(
            name="memory_append",
            description=(
                "Stage a wiki page append. The change will be merged into the wiki after "
                "all analysis workstreams complete. "
                "Only write what a future reviewer could not re-derive from the current code."
            ),
            schema=_APPEND_SCHEMA,
        )
        self._wiki_root = wiki_root
        self._pr_number = pr_number
        self._run_id = run_id

    async def process(self, arguments: dict) -> str:
        path = arguments["path"]
        content = arguments["content"]
        description = arguments["description"]

        project = _project_root(self._wiki_root)
        _validated, err = _resolve_safe_wiki_path(project, path)
        if err:
            return err
        tmp_path = project / "tmp" / f"pr{self._pr_number}-{self._run_id}" / path
        tmp_path.parent.mkdir(parents=True, exist_ok=True)

        frontmatter = (
            f"---\n"
            f"target: {path}\n"
            f"operation: append\n"
            f"description: {description}\n"
            f"run_id: {self._run_id}\n"
            f"---\n\n"
        )
        tmp_path.write_text(frontmatter + content, encoding="utf-8")
        return f"Staged append to {path}."


_STRUCTURE_UPSERT_SCHEMA = {
    "type": "object",
    "properties": {
        "module": {
            "type": "string",
            "description": "Canonical module key as repo path prefix (e.g., 'agent_core' or 'agent_core/nodes').",
        },
        "description": {
            "type": "string",
            "description": "Concise module description for structure.md.",
        },
        "evidence_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete repository paths supporting this description.",
        },
    },
    "required": ["module", "description", "evidence_paths"],
}


class _StructureUpsertTool(BoundTool):
    """Stages structure module upserts as JSON operation files."""

    def __init__(self, wiki_root: Path, pr_number: int, run_id: str) -> None:
        super().__init__(
            name="structure_upsert",
            description=(
                "Stage an update to structure.md module descriptions. "
                "Use module keys as repo path prefixes and include concrete evidence_paths."
            ),
            schema=_STRUCTURE_UPSERT_SCHEMA,
        )
        self._wiki_root = wiki_root
        self._pr_number = pr_number
        self._run_id = run_id

    async def process(self, arguments: dict) -> str:
        module = str(arguments.get("module", "")).strip().strip("/")
        description = str(arguments.get("description", "")).strip()
        evidence_paths = arguments.get("evidence_paths", [])

        if not module:
            return "Error: module is required."
        if "/" in module and any(part in {"", ".", ".."} for part in module.split("/")):
            return "Error: invalid module path."
        if not description:
            return "Error: description is required."
        if not isinstance(evidence_paths, list) or not evidence_paths or not all(isinstance(p, str) and p for p in evidence_paths):
            return "Error: evidence_paths must be a non-empty list of paths."

        project = _project_root(self._wiki_root)
        tmp_dir = project / "tmp" / f"pr{self._pr_number}-{self._run_id}" / "structure_ops"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        op = {
            "type": "structure_upsert",
            "module": module,
            "description": description,
            "evidence_paths": evidence_paths,
            "run_id": self._run_id,
        }
        filename = f"{module.replace('/', '__')}-{len(list(tmp_dir.glob('*.json'))):04d}.json"
        (tmp_dir / filename).write_text(json.dumps(op, ensure_ascii=True, sort_keys=True), encoding="utf-8")
        return f"Staged structure upsert for {module}."


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def make_memory_tools(wiki_root: Path) -> list[BoundTool]:
    """Direct canonical-access tools for Conversation and WikiMerge nodes."""
    return [
        _MemReadTool(wiki_root),
        _MemWriteTool(wiki_root),
        _MemAppendTool(wiki_root),
    ]


def make_analysis_memory_tools(
    wiki_root: Path,
    pr_number: int,
    run_id: str,
) -> list[BoundTool]:
    """Operation-file tools for Analysis nodes.

    memory_read is direct (Analysis must read canonical wiki to consult context).
    memory_write and memory_append are wrapped — they write to tmp/ and are applied
    by WikiMerge after the join gate.
    """
    return [
        _MemReadTool(wiki_root),
        _WrappedMemWriteTool(wiki_root, pr_number, run_id),
        _WrappedMemAppendTool(wiki_root, pr_number, run_id),
        _StructureUpsertTool(wiki_root, pr_number, run_id),
    ]
