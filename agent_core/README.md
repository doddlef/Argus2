# agent_core

Platform-agnostic domain layer for Argus. It knows nothing about GitHub — all
platform coupling lives in `github_connect` (above) and all LLM primitives live in
`llm_framework` (below).

```
github_connect   →   agent_core   →   llm_framework
                         │
                    (this package)
```

For the full design rationale and decision log, see [`docs/core_design.md`](../docs/core_design.md).

---

## Module map

```
agent_core/
├── config.py          Configuration dataclasses + load_config()
├── context.py         Three-layer context system (PipelineState / SessionContext / ApplicationContext)
├── dispatcher.py      Dispatcher + Trigger dataclasses (entry point from github_connect)
├── domain.py          Pure dataclasses: PRMetadata, Commit, ChangedFile, Plan, AnalysisReport, …
├── edges.py           PipelineState-aware edges: FanOutEdge, JoinEdge, PassThroughNode
├── pipelines.py       Pipeline factory functions: build_pr_workflow, build_comment_workflow
├── ports.py           Abstract port interfaces: CodeReader, Commenter
├── nodes/
│   ├── loading.py     PRLoadNode, AcknowledgeNode, MemLoadNode, TreeLoadNode, ThreadLoadNode
│   ├── analysis.py    TriageNode, RouteNode, AnalysisNode
│   ├── output.py      SummaryNode, WikiMergeNode
│   └── conversation.py ConversationNode
└── tools/
    ├── code.py        Repository navigation tools: list, read_diff, read_file, search
    └── memory.py      Wiki memory tools: memory_read, memory_write, memory_append
```

---

## Mental model

Every GitHub event becomes a `Trigger`, then flows through a `Workflow` as a
`PipelineState`. State carries two things:

- **`payload`** — accumulated loading-phase data (commits, file tree, wiki context, thread).
  Loading nodes mutate this in place. After the loading phase it is read-only.
- **`data`** — node-specific output (typed per stage). Each node reads `state.data`,
  does its work, and returns a new state with updated `data`.

### PR pipeline

```
None → PRLoad → Acknowledge → MemLoad → TreeLoad → ThreadLoad
     → Triage → [0 plans?] → Summary → WikiMerge
              → [N plans]  → FanOut → Analysis×N → Join → Summary → WikiMerge
```

- **Triage** produces a list of `Plan` objects. Empty list → Summary approves directly.
- **FanOut** fans `state.data` (the plan list) into N concurrent branches.
- **Analysis** runs a ReAct loop per branch; writes wiki observations to `tmp/` files.
- **Join** waits for all branches and merges them into `list[AnalysisReport]`.
- **Summary** computes verdict by rule (severity rank vs. `verdict_threshold` config),
  writes review body with one LLM call, posts the GitHub review.
- **WikiMerge** applies `tmp/` operation files to the canonical wiki.

### Comment pipeline

```
None → PRLoad → Acknowledge → MemLoad → TreeLoad → ThreadLoad → Route
     → "conversation" → Conversation → (done)
     → "analysis"     → Triage → … same as PR pipeline …
```

---

## Key types

| Type | File | Role |
|---|---|---|
| `PipelineState[T]` | `context.py` | State envelope passed between nodes |
| `PipelinePayload` | `context.py` | Loading-phase accumulator inside each state |
| `SessionContext` | `context.py` | Immutable per-run identifiers + port instances |
| `ApplicationContext` | `context.py` | Global singleton: clients, wiki_root, config, PR lock |
| `ClientTier` | `context.py` | Three LLM clients: `fast`, `standard`, `deep` |
| `ArgusConfig` | `config.py` | Parsed configuration (env > TOML > defaults) |
| `CodeReader` | `ports.py` | Abstract port for reading repo + PR data |
| `Commenter` | `ports.py` | Abstract port for posting comments and reviews |
| `Plan` | `domain.py` | One analysis workstream from Triage |
| `AnalysisReport` | `domain.py` | Output of one Analysis agent |

---

## Context access pattern

Nodes access immutable session data through a `ContextVar` — safe across asyncio
fan-out because each task inherits a snapshot of the context at the point
`create_task` is called.

```python
from agent_core.context import get_session

class MyNode(Node):
    async def execute(self, state: PipelineState[...]) -> PipelineState[...]:
        ctx = get_session()   # SessionContext: pr_number, reader, commenter, metadata, …
        files = await ctx.reader.fetch_changed_files(ctx.pr_number)
        ...
```

Never store `get_session()` at module level — always call it inside `execute()`.

---

## Configuration

Resolution order: **env > TOML > coded default**.

```toml
[argus]
wiki_root       = "/var/argus/wiki"   # required
bot_username    = "argus-bot"         # required

[clients.fast]
api_key         = "..."               # required — or ARGUS_FAST_API_KEY env var
model           = "claude-haiku-4-5-20251001"

[clients.standard]
api_key         = "..."               # required — or ARGUS_STANDARD_API_KEY
model           = "claude-sonnet-4-6"

[clients.deep]
api_key         = "..."               # required — or ARGUS_DEEP_API_KEY
model           = "claude-opus-4-7"

[limits]
max_plans                = 5    # Triage cap
max_inline_suggestions   = 10   # Summary keeps top-N by severity
max_file_window_lines    = 200  # read_file window cap
max_search_results       = 20   # search result cap
thread_compact_threshold = 10   # compact PR thread above this many new comments
preload_diff_threshold   = 300  # total diff lines below which diffs are pre-loaded

[review]
verdict_threshold = "high"   # critical | high | medium → request_changes; below → comment
```

All env var names follow `ARGUS_{SECTION}_{KEY}` (e.g. `ARGUS_MAX_PLANS`,
`ARGUS_REVIEW_DRAFTS`, `ARGUS_FAST_API_KEY`).

---

## Wiki filesystem layout

```
{wiki_root}/argus/{owner}/{repo}/
├── index.md              One-line entry per page; always loaded by MemLoad
├── structure.md          Codebase structure notes; always loaded by MemLoad
├── module-{name}.md      Per-module knowledge (on-demand via memory_read)
├── domain.md             Cross-cutting domain concepts
├── users/
│   └── {username}.md     Contributor context; written by Conversation + Analysis
├── pr/threads/
│   └── {pr_number}.md    Compacted thread summary (written by ThreadLoad)
└── tmp/
    └── pr{n}-{run_id}/   Staged operation files from Analysis agents (consumed by WikiMerge)
        └── {path}
```

**Analysis agents** write to `tmp/` (via wrapped tools); **WikiMerge** applies those
operations to canonical paths and deletes the files on success. Failed runs self-heal:
WikiMerge always scans all `tmp/pr{n}-*` files, including leftovers from prior runs.

---

## Memory tools — two modes

| Tool | Analysis nodes | Conversation / WikiMerge |
|---|---|---|
| `memory_read` | direct read | direct read |
| `memory_write` | → tmp operation file | → canonical page |
| `memory_append` | → tmp operation file | → canonical page |

The switch is made at construction time in `tools/memory.py`:

```python
# For Analysis nodes (staged writes):
make_analysis_memory_tools(wiki_root, pr_number, run_id)

# For Conversation / WikiMerge (direct writes):
make_memory_tools(wiki_root)
```

---

## Integrating with github_connect

`github_connect` is the only layer that imports from `agent_core`. It must:

1. Implement `CodeReader` and `Commenter` (defined in `agent_core/ports.py`).
2. Construct an `ApplicationContext` once at startup.
3. On each webhook event, build the appropriate `Trigger` and call `Dispatcher.dispatch()`.

```python
from agent_core.context import ApplicationContext, ClientTier, PRLockManager
from agent_core.config import load_config
from agent_core.dispatcher import Dispatcher, PROpenedTrigger

cfg = load_config()
app_ctx = ApplicationContext(
    clients=ClientTier(fast=..., standard=..., deep=...),
    wiki_root=cfg.wiki_root,
    pr_lock=PRLockManager(),
    bot_username=cfg.bot_username,
    config=cfg,
)

# On PR opened webhook:
trigger = PROpenedTrigger(
    installation_id=..., repo="owner/repo", pr_number=42,
    commit_sha="abc1234", actor="dev",
    reader=GitHubCodeReader(...),    # your CodeReader implementation
    commenter=GitHubCommenter(...),  # your Commenter implementation
)
await Dispatcher(app_ctx).dispatch(trigger)
```

The PR lock in `ApplicationContext` prevents concurrent processing of the same PR.
Keep the same `ApplicationContext` instance across all events for the lock to work.

---

## How to add a loading node

1. Create the class in `nodes/loading.py` — subclass `Node`, implement `execute()`.
   Input and output are `PipelineState[None]`; mutate `state.payload` in place.
2. Wire it into `build_pr_workflow` and `build_comment_workflow` in `pipelines.py`
   by inserting a `DirectedEdge` in the `edges` dict.

```python
# nodes/loading.py
class MyLoadNode(Node):
    async def execute(self, state: PipelineState[None]) -> PipelineState[None]:
        ctx = get_session()
        state.payload.my_field = await ctx.reader.fetch_something(ctx.pr_number)
        return state

# pipelines.py  (inside build_pr_workflow)
my_load = MyLoadNode()
...
edges = {
    ...
    threadload: DirectedEdge(my_load),   # insert before triage
    my_load:    DirectedEdge(triage),
    ...
}
```

---

## How to add a configuration key

1. Add the field to `ArgusConfig` (or `ClientConfig`) in `config.py` with a default.
2. Add a `r(...)` call in `load_config()` (or `_load_client()`) with the TOML path,
   env var name, default, and optional `cast=`.
3. Add the field access in any node that needs it — pass it at construction time from
   `app_ctx.config` in the pipeline factory.

---

## How to add a tool

1. Create a `BoundTool` subclass in `tools/code.py` or `tools/memory.py`.
2. Add it to the appropriate factory (`make_code_tools` or `make_memory_tools` /
   `make_analysis_memory_tools`).
3. The node that calls the factory will automatically include the new tool.

---

## Running the tests

```bash
# All tests
pytest tests/

# Pipeline wiring + behavior only
pytest tests/test_pipeline.py -v

# Config priority tests only
pytest tests/test_config.py -v
```

`tests/test_pipeline.py` uses `ScriptedClient` (queued `LLMResponse` objects) and
stub port implementations — no real API calls. Wiring tests inspect `workflow.edges`
directly; behavior tests run full pipelines via `Dispatcher`.

---

## Dependency summary

```
agent_core depends on:
  llm_framework   (LLMClient, Node, Edge, Workflow, BoundTool, Message, ToolResult, …)

agent_core is depended on by:
  github_connect  (Dispatcher, Trigger types, ApplicationContext, CodeReader, Commenter)

agent_core does NOT depend on:
  github_connect  (no upward import — ports are injected via Trigger)
  any HTTP / GitHub SDK
```
