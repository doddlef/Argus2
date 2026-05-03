# agent_core — Design Document

## Purpose

`agent_core` is the domain layer of Argus. It is **platform-agnostic** — it knows
nothing about GitHub. It defines the business logic, pipelines, ports, and context
system that sit between `llm_framework` (below) and `github_connect` (above).

```
github_connect → agent_core → llm_framework
```

The layering follows a controller / service split:

| Layer | Role |
|---|---|
| `github_connect` | Controller — receives GitHub webhooks, validates HMAC, translates JSON payloads into domain `Trigger` objects, constructs concrete port adapters, calls `Dispatcher.dispatch()` |
| `agent_core` | Service — `Dispatcher` receives `Trigger` objects, fetches PR metadata, constructs `SessionContext`, selects and runs the correct pipeline |

`github_connect` never contains routing or pipeline logic. `agent_core` never imports
from `github_connect`.

---

## 1. Ports

Ports are interfaces defined here and implemented by `github_connect`. They are the
only way `agent_core` communicates with the outside world.

### CodeReader

Read-only access to repository content and PR discussion.

```python
class CodeReader(ABC):
    async def fetch_pr_metadata(self, pr_number: int) -> PRMetadata: ...
    async def fetch_commits(self, pr_number: int) -> list[Commit]: ...
    async def fetch_changed_files(self, pr_number: int) -> list[ChangedFile]: ...
    async def fetch_diff(self, pr_number: int, path: str) -> str: ...
    async def fetch_file(self, path: str, ref: str) -> str: ...
    async def fetch_tree(self, ref: str) -> list[str]: ...        # flat blob paths only
    async def fetch_comments(self, pr_number: int, after: str | None = None) -> list[Comment]: ...
```

### Commenter

Write access to PR comments and reviews.

```python
class Commenter(ABC):
    async def post_comment(self, pr_number: int, body: str) -> str: ...      # returns comment_id
    async def post_review(
        self,
        pr_number: int,
        body: str,
        verdict: Literal["approve", "request_changes", "comment"],
        inline_comments: list[InlineComment],
    ) -> None: ...
```

Ports are provided via `SessionContext` and never constructed inside `agent_core`.

---

## 2. Domain Objects

Pure dataclasses. No logic, no dependencies.

```
PRMetadata      title, description, author, base_branch, head_branch,
                labels, pr_number, draft
Commit          sha, message
ChangedFile     path, status (added|modified|deleted), additions, deletions
Comment         id, author, body, created_at, is_inline, position
FileTree        paths: list[str], rendered: str
InlineComment   path, start_line, end_line, body
```

### InlineSuggestion

Produced by Analysis agents. Carries severity for Summary's filtering and verdict
logic. Converted to `InlineComment` (severity stripped) before passing to the
commenter port.

```python
@dataclass
class InlineSuggestion:
    path:       str
    start_line: int
    end_line:   int        # equal to start_line for single-line comments
    body:       str
    severity:   Literal["critical", "high", "medium", "low"]
```

### Plan

Produced by Triage. One plan per concurrent Analysis workstream.

```python
@dataclass
class Plan:
    files:         list[str]                            # assigned files, no overlap across plans
    focus:         str                                  # free-text label, e.g. "auth security"
    guidance:      str                                  # directive prompt for the Analysis agent
    tier:          Literal["fast", "standard", "deep"]
    preload_diffs: bool  # True if sum(additions+deletions) < preload_diff_threshold
```

### AnalysisReport

Produced by each Analysis agent. Always returned — failed runs set `failed=True`.

```python
@dataclass
class AnalysisReport:
    plan:               Plan
    findings:           str
    inline_suggestions: list[InlineSuggestion]
    severity:           Literal["critical", "high", "medium", "low", "none"]
    failed:             bool = False
    error:              str  = ""
```

### RouteDecision

```python
RouteDecision: Literal["conversation", "analysis"]
```

---

## 3. Context

Three layers, each with a distinct scope and mutability.

### State

The input/output of nodes and edges. Typed `Any` at the `llm_framework` level.
Most pipeline stages use `PipelineState[T]`, a generic wrapper that carries the
accumulated loading-phase data alongside the node-specific output. `PS[T]` is used
as shorthand in pipeline diagrams.

```python
@dataclass
class PipelinePayload:
    commits:         list[Commit]      = field(default_factory=list)
    changed_files:   list[ChangedFile] = field(default_factory=list)
    wiki_index:      str               = ""
    wiki_structure:  str               = ""
    file_tree:       FileTree | None   = None
    thread_summary:  str | None        = None
    recent_comments: list[Comment]     = field(default_factory=list)

@dataclass
class PipelineState(Generic[T]):
    payload: PipelinePayload
    data:    T
    fan_n:   int | None = None  # total fan-out branches; None outside fan sections
    fan_i:   int | None = None  # this branch's index (0-based); None outside fan sections
```

`PRLoad` receives `None` and creates the initial `PipelineState[None]` with an empty
payload. Loading nodes receive and return `PipelineState[None]`, updating payload
fields in place (loading is sequential — no write-safety concern). `fan_n`/`fan_i`
are set by `FanOutEdge` and cleared by `JoinEdge`; they are `None` in all other
stages. `FanOutEdge` fans on `state.data` (the list) and copies `state.payload` into
each branch state. See §8 for the full per-stage flow.

### SessionContext

Immutable identifiers and ports, set by the Dispatcher before the workflow runs.
Fully frozen — no mutable compartment.

```python
@dataclass(frozen=True)
class SessionContext:
    installation_id: int
    repo:            str          # "owner/repo"
    pr_number:       int
    commit_sha:      str
    actor:           str          # GitHub user who triggered the event
    reader:          CodeReader
    commenter:       Commenter
    metadata:        PRMetadata   # fetched by Dispatcher before pipeline starts
```

Access via `ContextVar` — safe across asyncio fan-out because `create_task` copies
the current context snapshot into each branch:

```python
_session: ContextVar[SessionContext] = ContextVar("session")

@asynccontextmanager
async def session(ctx: SessionContext):
    token = _session.set(ctx)
    try:
        yield ctx
    finally:
        _session.reset(token)

def get_session() -> SessionContext:
    return _session.get()   # raises LookupError if not set — intentional fast-fail
```

All nodes access immutable session data (ports, metadata, identifiers) via
`get_session()`. Accumulated loading data flows through `PipelineState.payload`.

### ApplicationContext

Global singleton constructed at startup. Never changes after initialisation.

```python
@dataclass(frozen=True)
class ApplicationContext:
    clients:      ClientTier
    wiki_root:    Path           # server filesystem path for wiki files
    pr_lock:      PRLockManager  # asyncio.Lock keyed by (installation_id, pr_number)
    bot_username: str            # GitHub username Argus posts as
    config:       ArgusConfig    # parsed TOML configuration

@dataclass(frozen=True)
class ClientTier:
    fast:     LLMClient   # triage, route, acknowledge
    standard: LLMClient   # conversation, summary, wiki_merge
    deep:     LLMClient   # analysis (overridable per Plan)
```

Every client is automatically wrapped in `RetryClient` at construction. Nodes never
interact with an unwrapped client.

### Configuration

Three-layer resolution at startup:

1. `ARGUS_CONFIG_PATH` env var (or default path) → load TOML file
2. Parameters missing from TOML → read environment variables
3. Parameters missing from env → use coded defaults, or raise on required fields

---

## 4. Concurrency

Events for the same PR are processed **sequentially** via a per-PR async lock.

```python
async with app_ctx.pr_lock(installation_id, pr_number):
    async with session(ctx):
        await workflow.run(None)
```

Within a single pipeline run, Analysis agents run **concurrently** via fan-out. Write
safety is guaranteed by the operation-file pattern: each agent writes to isolated tmp
files. WikiMerge applies all operations after the join gate (see §5 and §9).

---

## 5. Memory Tools

Three tools used by all agent nodes. Behaviour differs by node via tool wrapping.

```python
memory_read(path: str) -> str
# Read any wiki page. Returns error string if page does not exist.

memory_write(path: str, content: str, description: str) -> str
# Create or overwrite a wiki page.
# description: one-line index entry written to index.md for this path.

memory_append(path: str, content: str, description: str) -> str
# Append content to an existing wiki page (creates it if absent).
# description: updates the index.md entry for this path.
```

### Write discipline (baked into Analysis and WikiMerge system prompts)

> *"Before calling `memory_write` or `memory_append`, ask: would a future reviewer
> be wrong or inefficient without this? If they could figure it out in 30 seconds
> from the code, skip it."*

| Worth writing | Not worth writing |
|---|---|
| Architectural intent behind a non-obvious design | Anything visible in current code |
| A recurring bug pattern | Anything derivable from git history |
| A known performance hotspot | Function signatures or file contents |
| A team norm or constraint invisible in code | Findings specific to one PR |

### Operation file pattern (Analysis nodes)

Analysis agents' `memory_write` and `memory_append` tools are **wrapped** at
`AnalysisNode` construction time. The wrapper intercepts calls and writes an
**operation file** to `tmp/` instead of the canonical path:

```
tmp/pr{pr_number}-{run_id}-{path}
```

Operation file format:

```markdown
---
target:      module-auth.md
operation:   write | append
description: auth module — JWT lifecycle, race condition in token refresh
run_id:      a3f9c2d8
---

{content from the agent's call}
```

The agent always calls `memory_write("module-auth.md", content, description)`. The
wrapper transparently rewrites to the tmp namespace. The agent is unaware of tmp pages.

`WikiMerge` reads all operation files after the join gate and applies them to the
canonical wiki. Conversation and WikiMerge nodes receive unwrapped tools with direct
canonical access.

---

## 6. Memory / Wiki

Filesystem-based, no embeddings, no RAG. All wiki files live under:

```
{wiki_root}/argus/{repo_owner}/{repo_name}/
```

### Always-loaded pages (every pipeline run, via MemLoad)

| File | Contents |
|---|---|
| `index.md` | One entry per wiki page: `- path — one-line description` |
| `structure.md` | Codebase structure: top-level modules, critical file paths, known hotspots |

Keep the always-loaded set under ~2 K tokens. `index.md` entries are one line each,
under 150 characters: `- module-auth.md — auth module: JWT lifecycle, token refresh`.

`structure.md` follows a hybrid ownership model:
- Human-authored sections (`Modules`, `Runtime Flow`, `Conventions`) are the primary
  architecture guidance.
- Automation maintains only the auto-generated paths and update metadata sections.
- Analysis agents do not write the canonical file directly; they stage
  `structure_upsert` proposals that are merged later by WikiMerge.

### On-demand pages (via `memory_read` tool)

| File pattern | Contents |
|---|---|
| `module-{name}.md` | Per-module knowledge |
| `domain.md` | Cross-cutting domain concepts and invariants |
| `users/{username}.md` | Contributor context — both Analysis and Conversation can write these |

Every on-demand page carries staleness metadata in its frontmatter:

```markdown
---
last_updated_pr:  47
last_updated_sha: a3f9c2d
---
```

### PR thread compaction

| File | Written by | Contents |
|---|---|---|
| `pr/threads/{pr_id}.md` | ThreadLoad (compact path) | Compacted thread summary + `last_comment_id` marker |

Compaction file format:

```markdown
---
last_comment_id:   "ic_8f3a2c"
compacted_at_sha:  a3f9c2d
---

{free-text summary: decisions made, unresolved questions, Argus's last verdict,
key discussion points. Argus Acknowledge comments excluded.}
```

When a PR thread is loaded:
1. Read `pr/threads/{pr_id}.md` if it exists → extract `last_comment_id`
2. Fetch only comments after `last_comment_id` (or all if no file)
3. If `new_comments > thread_compact_threshold` (config, default 10) →
   fast-client compact call → overwrite file with new summary + updated `last_comment_id`
4. Deliver `(thread_summary, recent_comments)` to the pipeline via `ctx.payload`

### Tmp operation files

During each pipeline run, Analysis agents write operation files to:

```
tmp/pr{pr_number}-{run_id}-{canonical_path}
```

WikiMerge scans all `tmp/pr{pr_number}-*` files (including any from previous failed
runs — self-healing) after Summary completes. It applies all operations and deletes
the tmp files.

---

## 7. File Tree

The full repository file tree is pre-loaded in `TreeLoad` via one GitHub API call.
`list(path)` is an in-memory filter over this — zero additional API calls.

### Pre-simplification (static, no LLM)

Build a trie from all blob paths. Collapse any internal node that has exactly one
child and is not itself a file. This removes deep single-child chains common in Java
projects (`src/main/java/com/example/` → rendered as one segment).

### Rendering for prompts

- Collapsed tree rendered to max depth 4 below the trie root
- Nodes beyond depth 4 shown as `path/ (N files)` — agent uses `list()` to drill in
- Full paths always stored internally for accurate `list(path)` prefix filtering

---

## 8. Pipelines

`PS[T]` = `PipelineState[T]` throughout the diagrams below.

### State flow — PR Opened / PR Sync

```
None
 → PRLoad         → PS[None]               (payload: commits, changed_files)
 → Acknowledge    → PS[None]               (side effect: post_comment)
 → MemLoad        → PS[None]               (payload: wiki_index, wiki_structure)
 → TreeLoad       → PS[None]               (payload: file_tree)
 → ThreadLoad     → PS[None]               (payload: thread_summary, recent_comments)
 → Triage         → PS[list[Plan]]
 → FanOutEdge     → PS[Plan]               (fan_n=N, fan_i=i; concurrent)
 → Analysis       → PS[AnalysisReport]
 → JoinEdge       → PS[list[AnalysisReport]]  (fan_n/fan_i cleared)
 → Summary        → None                   (side effect: post_review)
 → WikiMerge      → None                   (side effect: merge operation files into wiki)
```

### State flow — Comment Received

```
None
 → PRLoad         → PS[None]
 → Acknowledge    → PS[None]               (side effect: post_comment)
 → MemLoad        → PS[None]
 → TreeLoad       → PS[None]
 → ThreadLoad     → PS[None]
 → Route          → PS[RouteDecision]
     "conversation" → Conversation → None  (side effect: post_comment)
     "analysis"    → Triage → PS[list[Plan]] → FanOut → PS[Plan] → Analysis
                    → PS[AnalysisReport] → Join → PS[list[AnalysisReport]]
                    → Summary → None
                    → WikiMerge → None
```

---

## 9. Node Reference

### Loading Phase

#### PRLoad
- **Responsibility**: fetch `commits` and `changed_files`; create initial `PipelineState`
- **LLM**: no
- **Input / Output**: `None → PipelineState[None]`  (creates initial state with empty payload; populates commits, changed_files)
- **Failure**: abort pipeline

#### Acknowledge
- **Responsibility**: post an instant comment so the author knows Argus is working.
  Template varies by trigger type; configurable via `acknowledge_events` (default on).
- **Templates**:
  - `PROpenedTrigger` / `PRSyncTrigger`: `"Argus is reviewing this PR. I'll post my findings shortly."`
  - `CommentReceivedTrigger`: `"On it — I'll respond shortly."`
- **LLM**: no
- **Input / Output**: `PipelineState[None] → PipelineState[None]`  (side effect: post_comment)
- **Failure**: log and continue

#### MemLoad
- **Responsibility**: read `index.md` and `structure.md` from the wiki filesystem;
  write to `state.payload.wiki_index` and `state.payload.wiki_structure`
- **LLM**: no
- **Input / Output**: `PipelineState[None] → PipelineState[None]`
- **First run**: sets both fields to `""` — pipeline continues without wiki context

#### TreeLoad
- **Responsibility**: fetch full file tree; apply pre-simplification; store
  `FileTree` in `state.payload.file_tree`
- **LLM**: no
- **Input / Output**: `PipelineState[None] → PipelineState[None]`
- **Failure**: continue degraded — `list()` tool returns an error string for this run

#### ThreadLoad
- **Responsibility**: load PR thread; run compaction if over threshold (Compact path);
  write `thread_summary` and `recent_comments` to `state.payload`
- **LLM**: conditional (compact path only) — `fast` client
- **Input / Output**: `PipelineState[None] → PipelineState[None]`
- **Failure**: continue with `thread_summary=None`, `recent_comments=[]`

---

### Analysis Phase

#### Triage
- **Responsibility**: produce `list[Plan]` from PR context. Groups related files into
  workstreams, assigns focus, writes guidance prompts, picks client tier per plan,
  sets `preload_diffs` flag. Plans sorted by risk descending. Output capped at
  `max_plans` (config, default 5) after the tool call — prompt instructs ≤ max_plans.
  Returns empty list for trivial PRs (docs-only, no code changes).
- **LLM**: yes — single structured call, tool-forcing via `submit_plans`
- **Client**: fast
- **Tools**: `submit_plans(plans: list[Plan])` only
- **Sees**: `get_session().metadata`, `state.payload.commits`, `state.payload.changed_files`,
  `state.payload.wiki_index`, `state.payload.wiki_structure`
- **Constraint**: never reads full file content — only file list and patch stats
- **Input / Output**: `PipelineState[None] → PipelineState[list[Plan]]`

#### Analysis  *(one per Plan, run concurrently)*
- **Responsibility**: deep investigation of assigned files. Autonomously navigates
  code via tools, consults wiki for codebase context, writes wiki observations as
  operation files. Does NOT post any comment.
- **LLM**: yes — ReAct loop, max 20 iterations
- **Client**: tier from `Plan` (`fast` / `standard` / `deep`)
- **Tools**:
  - `list(path, pattern=None)` — in-memory tree filter, zero API calls
  - `read_diff(path)` — patch for one file
  - `read_file(path, start_line, end_line)` — chunked, line-numbered content
  - `search(pattern, path=None)` — substring grep, bounded excerpts
  - `memory_read(path)` — read a wiki page
  - `memory_write(path, content, description)` — write operation file (tmp)
  - `memory_append(path, content, description)` — append operation file (tmp)
- **Initial message includes**:
  - PR metadata (title, description, author, head_branch)
  - Commit messages
  - Plan (files, focus, guidance)
  - Pre-loaded diff content for all plan files (if `plan.preload_diffs=True`)
  - `wiki_index` + `wiki_structure`
  - `thread_summary` + `recent_comments`
- **Side effect**: writes `tmp/pr{pr_number}-{run_id}-{path}` operation files
- **Input / Output**: `PipelineState[Plan] → PipelineState[AnalysisReport]`  (`failed=True` on failure)
- **Self-configuration**: `AnalysisNode` receives `ClientTier` and base tools at
  construction. In `run(state)`, it reads `state.data` for the `Plan`, selects the
  right client and wraps the memory tools with a per-run UUID prefix. No factory
  needed for FanOutEdge.

#### Route  *(comment pipeline only)*
- **Responsibility**: classify the triggering comment as `"conversation"` or
  `"analysis"`. Default: `"conversation"` when uncertain.
- **LLM**: yes — single structured call, tool-forcing via `submit_decision`
- **Client**: fast
- **Sees**: `CommentReceivedTrigger.comment_body`, last 5 from
  `state.payload.recent_comments`, `get_session().metadata.title`
- **Input / Output**: `PipelineState[None] → PipelineState[RouteDecision]`

#### Conversation  *(comment pipeline, conversation branch)*
- **Responsibility**: directly answer a question or respond to the thread. Loads
  relevant wiki pages and reads code as needed. Posts reply directly. May update
  `users/` wiki pages with observed contributor context.
- **LLM**: yes — ReAct loop, max 10 iterations
- **Client**: standard
- **Tools**: `list`, `read_diff`, `read_file`, `search`,
  `memory_read`, `memory_write` (direct), `memory_append` (direct)
- **Side effects**: calls `commenter.post_comment()`; may write canonical wiki pages
- **Input / Output**: `PipelineState[None] → None`  (terminal)

---

### Output Phase

#### Summary
- **Responsibility**: synthesise all `AnalysisReport` objects into a top-level review.
  Determine overall verdict. Surface cross-cutting patterns. Note failed workstreams.
  Post the GitHub review.
- **Verdict logic** (rule-based):
  1. Compute `effective_severity = max severity across all non-failed reports and
     their inline_suggestions`
  2. Apply `verdict_threshold` (config, default `"high"`):
     `≥ threshold → request_changes`, `< threshold → comment`, no findings → `approve`
- **Inline comment filtering**: sort all `InlineSuggestion` objects by severity
  descending; keep top `max_inline_suggestions` (config, default 10). Review body
  notes total count if truncated.
- **Reads**: `get_session().metadata`, `state.payload.thread_summary`, `state.payload.recent_comments`
  for continuity context (avoids re-flagging resolved issues)
- **LLM**: yes — single call, no ReAct loop
- **Client**: standard
- **Side effects**:
  - Calls `commenter.post_review()` with verdict + inline comments + review body
- **Input / Output**: `PipelineState[list[AnalysisReport]] → None`
- **On failure**: post static error comment + clean up all `tmp/pr{pr_number}-*` files

#### WikiMerge
- **Responsibility**: apply all Analysis operation files to the canonical wiki.
  Deduplicate, resolve contradictions, update index coherently. Self-healing: picks
  up operation files from previous failed runs.
- **LLM**: yes — ReAct loop, standard client
- **Tools**: `memory_read`, `memory_write` (direct), `memory_append` (direct)
- **Process**:
  1. Scan all `tmp/pr{pr_number}-*` operation files (includes any from prior failed runs)
  2. Group by `target` canonical path
  3. One ReAct agent processes all pages (cross-page awareness, index coherence)
  4. For each target: load existing canonical page + all operations → merge intelligently
  5. Apply write discipline: discard anything derivable from current code or git history
  6. Update index.md last; delete all processed tmp files
- **Input / Output**: None → None (terminal)
- **On failure**: log error — operation files self-heal on next pipeline run

---

## 10. Tool Reference

```python
list(path: str, pattern: str | None = None) -> list[str]
# Returns full paths (relative to repo root) for all entries under path.
# Bounded by pre-simplified tree (depth cap 4).
# pattern: optional glob filter on filenames (e.g. "*.py").
# Returns empty list if path doesn't exist — no error.

read_diff(path: str) -> str
# Returns the diff patch for one changed file in this PR.
# Error string if file is not in the diff.

read_file(path: str, start_line: int, end_line: int) -> str
# Line-numbered output: "42: def foo():\n43:     pass\n"
# Always reads at SessionContext.commit_sha.
# Max window: max_file_window_lines (config, default 200 lines).
# Truncated with notice if window exceeded.
# Clamped with informative message if range is out of bounds.
# One internal retry on transient API errors; error as value on persistent failure.

search(pattern: str, path: str | None = None) -> str
# Substring search (not regex) across the repository (or scoped to path).
# Returns: "path/file.py:42: matching line\n..." (grep-style).
# Bounded at max_search_results (config, default 20).
# Truncation notice appended if results exceed cap.

memory_read(path: str) -> str
# Read any wiki page by path relative to the project wiki root.
# Error string if page does not exist.

memory_write(path: str, content: str, description: str) -> str
# Create or overwrite a wiki page.
# Updates index.md entry: "- {path} — {description}".
# Analysis nodes: wrapped — produces operation file in tmp/.
# Conversation / WikiMerge: direct canonical write.

memory_append(path: str, content: str, description: str) -> str
# Append content to an existing wiki page (creates if absent).
# Updates index.md entry with new description.
# Analysis nodes: wrapped — produces append operation file in tmp/.
# Conversation / WikiMerge: direct canonical write.
```

All tool errors are returned as **descriptive strings**, never as exceptions. The
ReAct loop receives the error message and can try a different approach. One internal
retry for transient API errors before surfacing the error string.

---

## 11. Tool Allocation Summary

| Node | list | read\_diff | read\_file | search | memory\_read | memory\_write | memory\_append |
|---|---|---|---|---|---|---|---|
| Triage | — | — | — | — | — | — | — |
| Analysis | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (op file) | ✓ (op file) |
| Route | — | — | — | — | — | — | — |
| Conversation | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (direct) | ✓ (direct) |
| Summary | — | — | — | — | — | — | — |
| WikiMerge | — | — | — | — | ✓ | ✓ (direct) | ✓ (direct) |

`commenter` is never a tool. Nodes call it directly when they own the write
responsibility. Exactly two nodes touch `commenter` per PR pipeline run:
`Acknowledge` (post_comment) and `Summary` (post_review). In the comment pipeline,
`Conversation` replaces `Summary` on the conversation branch.

---

## 12. Dispatcher and Triggers

`Dispatcher` and all `Trigger` types live in `agent_core`.

### Triggers

```python
@dataclass(frozen=True)
class PROpenedTrigger:
    installation_id: int
    repo:            str
    pr_number:       int
    commit_sha:      str
    actor:           str
    reader:          CodeReader
    commenter:       Commenter

@dataclass(frozen=True)
class PRSyncTrigger:
    # identical fields to PROpenedTrigger

@dataclass(frozen=True)
class CommentReceivedTrigger:
    installation_id: int
    repo:            str
    pr_number:       int
    commit_sha:      str
    actor:           str
    comment_id:      str
    comment_body:    str
    reader:          CodeReader
    commenter:       Commenter
```

### Dispatcher

```python
class Dispatcher:
    def __init__(self, app_ctx: ApplicationContext) -> None: ...

    async def dispatch(
        self,
        trigger: PROpenedTrigger | PRSyncTrigger | CommentReceivedTrigger,
    ) -> None:
        async with self._app_ctx.pr_lock(trigger.installation_id, trigger.pr_number):
            metadata = await trigger.reader.fetch_pr_metadata(trigger.pr_number)

            if metadata.draft and not self._app_ctx.config.review_drafts:
                await trigger.commenter.post_comment(
                    trigger.pr_number,
                    "Draft PR detected — Argus will review when marked ready.",
                )
                return

            ctx = SessionContext(
                installation_id=trigger.installation_id,
                repo=trigger.repo,
                pr_number=trigger.pr_number,
                commit_sha=trigger.commit_sha,
                actor=trigger.actor,
                reader=trigger.reader,
                commenter=trigger.commenter,
                metadata=metadata,
            )
            async with session(ctx):
                workflow = self._build_workflow(trigger)
                await workflow.run(None)

    def _build_workflow(self, trigger) -> Workflow:
        match trigger:
            case PROpenedTrigger() | PRSyncTrigger():
                return build_pr_workflow(self._app_ctx)
            case CommentReceivedTrigger():
                return build_comment_workflow(self._app_ctx)
```

`workflow.run(None)` seeds the pipeline with `None`; `PRLoad` creates the initial
`PipelineState[None]`. All nodes access immutable session data via `get_session()`.
State carries only node-to-node data — trigger fields and session context never flow
through State.

---

## 13. Failure Handling

| Node | Failure behaviour |
|---|---|
| PRLoad | Abort — no review posted |
| Acknowledge | Log and continue — non-critical |
| MemLoad | Continue with `wiki_index=""`, `wiki_structure=""` |
| TreeLoad | Continue degraded — `list()` returns error string for this run |
| ThreadLoad | Continue with `thread_summary=None`, `recent_comments=[]` |
| Triage | Abort — no plans, no review |
| Analysis (one of N) | Returns `AnalysisReport(failed=True)` — partial results |
| Summary | Static error comment + clean up all `tmp/pr{pr_number}-*` files |
| WikiMerge | Log error — operation files self-heal on next run |

On any abort, the pipeline exits without posting a review. The author sees no comment
(PRLoad abort) or an error comment (Summary abort).

---

## 14. Configuration

```toml
[argus]
wiki_root          = "/var/argus/wiki"
bot_username       = "argus-bot"
review_drafts      = false        # if false, skip draft PRs
acknowledge_events = true         # if false, Acknowledge node is a no-op

[limits]
max_plans                = 5      # Triage cap; enforced by prompt + application
max_inline_suggestions   = 10     # Summary filters to top-N by severity
max_search_results       = 20     # search() result cap
max_file_window_lines    = 200    # read_file() window cap
thread_compact_threshold = 10     # compact thread when new_comments exceeds this
preload_diff_threshold   = 300    # total diff lines below which Triage sets preload_diffs=True

[review]
verdict_threshold = "high"        # critical | high | medium
                                  # findings at or above this → request_changes

[clients.fast]
provider = "anthropic"
model    = "claude-haiku-4-5"

[clients.standard]
provider = "anthropic"
model    = "claude-sonnet-4-6"

[clients.deep]
provider = "anthropic"
model    = "claude-opus-4-7"
```

---

## 15. Background Maintenance

**Deferred — future story.**

The background maintenance workflow (`WikiScan → Audit → WikiCleanup`) will audit
the wiki for stale pages, orphans, index gaps, and contradictions. It requires a
`BackgroundMaintenanceTrigger` (with injected `CodeReader`, no `SessionContext`)
and a `staleness_commits` config value.

Trigger mechanism is `github_connect`'s responsibility (cron, post-merge hook, or
comment command `@argus audit-wiki`).

---

## 16. Key Decisions

| # | Decision | Rationale |
|---|---|---|
| C01 | Per-PR sequential queue | Avoids double-posting; matches single-focused-reviewer mental model |
| C02 | `SessionContext` fully frozen; `PipelinePayload` carried in `PipelineState` | Truly immutable session context; payload flows explicitly through state edges |
| C03 | Tiered clients (fast / standard / deep) | Predictable cost; each node picks the tier its complexity warrants |
| C04 | Loading phase nodes return `PipelineState[None]`; mutate `state.payload` in place | Payload flows through state explicitly; SessionContext remains truly immutable |
| C05 | Only `Acknowledge`, `Summary`, `Conversation` touch `commenter` | One write path per event; simplifies retry and prevents double-posting |
| C06 | Analysis does not post comments | GitHub review coherence; one formal review is cleaner than scattered comments |
| C07 | Concurrent Analysis + operation files → WikiMerge | Concurrent execution preserved; write safety via isolated tmp files; WikiMerge has cross-page awareness |
| C08 | `list()` filters pre-loaded tree in memory | Zero additional API calls; full navigation available to all agents |
| C09 | Tree pre-simplification: collapse single-child chains, depth cap 4 | Reduces prompt size for deep package structures without losing accuracy |
| C10 | Thread compaction inside `ThreadLoad` | Caller pipeline is unaware of whether compaction ran; always receives a usable thread |
| C11 | Always-loaded wiki set ≤ 2 K tokens | Prevents index/structure from crowding out diff content |
| C12 | No Summary wiki recording | WikiMerge handles wiki updates; Summary focuses on the review |
| C13 | `Dispatcher` and `Trigger` types live in `agent_core` | Routing logic is domain knowledge; platform-agnostic |
| C14 | Ports injected via `Trigger` fields | No upward import; github_connect fulfils ABCs defined in agent_core |
| C15 | Background maintenance deferred | Out of scope for initial implementation; `BackgroundMaintenanceTrigger` pattern defined for future use |
| C16 | `PRMetadata` fetched by Dispatcher, stored in `SessionContext` | Available to all nodes without a loading step; eliminates PRLoad metadata fetch |
| C17 | Three memory tools with `description` parameter | Index update bundled into write/append — no separate index tool; description is the authoritative index entry |
| C18 | `InlineSuggestion` with `start_line` / `end_line` / `severity`; `InlineComment` strips severity | Precision for multi-line issues; severity is domain-internal for verdict/filtering only |
| C19 | Analysis memory tools wrapped to produce operation files | Agent writes as if to canonical paths; wrapper handles tmp namespace transparently |
| C20 | WikiMerge is a ReAct agent (not N separate calls) | Cross-page awareness prevents contradictions; index coherence managed in one pass |
| C21 | WikiMerge self-healing — processes all `tmp/pr{n}-*` regardless of run_id | Failed runs self-heal on next event; no orphan cleanup needed |
| C22 | Verdict rule-based from `effective_severity`; threshold configurable | Deterministic; avoids LLM judgment drift across runs |
| C23 | Triage uses tool-forcing (`submit_plans`); Route uses `submit_decision` | Reliable structured output for any provider; same pattern reused |
| C24 | 0-plan Triage is valid — Summary approves with lightweight message | Handles docs-only PRs gracefully; no special case needed |
| C25 | Analysis initial message includes full context bundle | Agent starts with maximum relevant context; reduces tool-call round-trips |
| C26 | `AnalysisReport` has `failed` flag — Analysis always returns a report | JoinEdge type stays `list[AnalysisReport]`; Summary knows which workstreams failed |
| C27 | Draft PRs handled at Dispatcher level via `review_drafts` config | Cheap check before pipeline starts; no wasted LLM calls on incomplete code |
| C28 | `Acknowledge` configurable (`acknowledge_events`) with per-trigger templates | Teams that find it noisy can disable; templates vary by event type |
| C29 | `AnalysisNode` self-configures from `Plan` at `run()` time | No factory callable needed for FanOutEdge; single node instance reused safely across concurrent runs |
| C30 | PR sync runs full re-review; thread compaction provides continuity | Simple pipeline; avoids missed indirect changes; Summary uses thread history to avoid re-flagging resolved issues |
| C31 | FanOut/Join coordinate via `fan_n`/`fan_i` fields in `PipelineState` | Self-describing state; no external counters or sync primitives; Join orders results correctly by `fan_i` |
