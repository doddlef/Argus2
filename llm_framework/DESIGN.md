# llm_framework — Design Document

## Purpose

`llm_framework` is the capability layer of Argus. It is **model-agnostic** and
**domain-agnostic** — it knows nothing about GitHub, pull requests, or code review.

It provides three things:
1. **Data types** — the vocabulary for talking to LLMs and moving data through pipelines
2. **LLM client abstraction** — a unified interface over multiple providers
3. **Execution primitives** — a ReAct agent loop and a generic async workflow engine

Upper layers (`agent_core`, `github_connect`) depend on this module.
This module depends on nothing above itself.

---

## Dependency Direction

```
github_connect → agent_core → llm_framework
```

---

## 1. Messages (`messages.py`)

### Content blocks

Message content is a typed union of blocks, not a raw string or `list[dict]`.
Using `list[dict]` would leak Anthropic's wire format into the domain model and
break portability across providers.

```
ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock
```

| Block | Fields | Purpose |
|---|---|---|
| `TextBlock` | `text: str`, `cache: bool = False` | Plain text content |
| `ToolUseBlock` | `id: str`, `name: str`, `input: dict` | Tool call made by the model |
| `ToolResultBlock` | `tool_use_id: str`, `content: str`, `is_error: bool = False` | Result fed back to the model |

`cache: bool` on `TextBlock` maps to `cache_control: {"type": "ephemeral"}` in
AnthropicClient. Other providers ignore it. The field is provider-neutral in intent
(mark expensive-to-recompute content) even if only Anthropic acts on it today.

### Message

```python
@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: str | list[ContentBlock]
```

`str` is the shorthand for simple text messages. `list[ContentBlock]` is used when
the message carries tool calls or results.

**Factory methods:**
- `Message.text(role, text)` — convenience constructor for plain text
- `Message.from_tool_results(results: list[ToolResult])` — builds the user-role
  message that feeds tool results back to the model. Takes `list[ToolResult]` (not
  `list[str]`) so that `is_error` is preserved. Does not require the original
  `ToolCall` list — `ToolResult.id` already carries the matching ID.

---

## 2. Tools (`tools.py`)

### BoundTool

`Tool` and `BoundTool` are merged into a single type. A tool is always defined
alongside its implementation — there is no use case for a schema-only tool object.

```python
@dataclass
class BoundTool:
    name: str
    description: str
    schema: dict          # JSON Schema for the parameters

    async def process(self, arguments: dict) -> str:
        raise NotImplementedError
```

`schema` stays as `dict` — JSON Schema is complex enough that wrapping it in a
type hierarchy would be over-engineering.

Concrete tools subclass `BoundTool` and override `process()`. This pattern
supports injected dependencies (e.g. a `CodeReader` port) cleanly, unlike the
`execute: Callable` approach which gets messy with non-serializable callables.

`LLMClient.complete()` receives `list[BoundTool]` and reads only `name`,
`description`, `schema`. It never calls `process()`.

### ToolCall

```python
@dataclass
class ToolCall:
    id: str           # Anthropic requires matching call ID to result
    name: str
    arguments: dict
```

### ToolResult

```python
@dataclass
class ToolResult:
    id: str           # matches ToolCall.id
    content: str
    is_error: bool = False
```

`is_error` is a first-class field. Anthropic's API supports `is_error: true` on
tool results and the model reasons differently when it sees one.

---

## 3. Responses (`responses.py`)

### CompleteOptions

Per-call overrides for model behaviour. Stored on the `Agent` instance; callers
can pass a different `CompleteOptions` to `run()` if needed.

```python
@dataclass
class CompleteOptions:
    max_tokens: int = 8096
    temperature: float | None = None
```

### LLMResponse

```python
@dataclass
class LLMResponse:
    content: str | None         # None when the model responds with only tool calls
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: Literal['end_turn', 'tool_use', 'max_tokens'] = 'end_turn'

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_message(self) -> Message:
        ...
```

`to_message()` converts the response back into an assistant `Message` for history
reconstruction inside the agent loop. This is the symmetric counterpart to
`Message.from_tool_results()`.

---

## 4. LLM Client (`client.py`)

### LLMClient ABC

```python
class LLMClient(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[BoundTool] | None = None,
        system: str | None = None,
        options: CompleteOptions | None = None,
    ) -> LLMResponse:
        ...
```

`system` is a top-level parameter (not embedded in messages) because every provider
conceptually has a system prompt. `AnthropicClient` passes it directly to the API;
other providers may prepend it as a message internally.

### RetryClient

Wraps any `LLMClient` and retries on transient errors (rate limits, 5xx). Retry
logic lives here — not inside individual provider clients — so it is testable
independently and not duplicated across providers.

```python
class RetryClient(LLMClient):
    def __init__(self, inner: LLMClient, max_retries: int = 3): ...
```

The `ApplicationContext` factory (in `agent_core`) wraps every provider client in
`RetryClient` automatically. Callers never interact with an unwrapped client.

---

## 5. Providers (`providers/`)

### AnthropicClient

Implements `LLMClient` using the `anthropic` SDK.

- Maps `TextBlock.cache = True` → `cache_control: {"type": "ephemeral"}`
- Maps `BoundTool` → Anthropic tool definition format
- Maps Anthropic response → `LLMResponse` with typed `ToolCall` objects

### OllamaClient

Implements `LLMClient` using the Ollama HTTP API.

- Ignores `cache` fields on content blocks
- Prepends `system` as a message if the API does not support a top-level field

### MockLLMClient

Scriptable test double. Consumes a pre-loaded queue of `LLMResponse` objects.
Raises if the queue is empty and `complete()` is called (catches over-calling).
Exposes `self.calls: list[list[Message]]` so tests can assert what the agent sent.

```python
class MockLLMClient(LLMClient):
    def __init__(self, responses: list[LLMResponse]): ...
    calls: list[list[Message]]
```

---

## 6. Agent (`agent.py`)

A concrete, composable ReAct loop executor. **Not a base class** — nodes in
`agent_core` hold an `Agent` instance and call `run()` directly. System prompts
and domain translation live in the node, not in the agent.

```python
class Agent:
    def __init__(
        self,
        client: LLMClient,
        tools: list[BoundTool],
        system: str | None = None,
        max_iterations: int = 10,
        options: CompleteOptions | None = None,
    ): ...

    async def run(self, messages: list[Message]) -> LLMResponse:
        ...
```

### Loop behaviour

1. Copy `messages` into a local `history` (never mutates caller's list)
2. Call `client.complete(history, tools, system, options)`
3. If `stop_reason == 'end_turn'` or no tool calls → return response
4. Append `response.to_message()` to history
5. Dispatch all tool calls **concurrently** via `asyncio.gather()`
6. Catch all tool exceptions; wrap as `ToolResult(is_error=True)` — the model
   sees the error and can adapt rather than the loop crashing
7. Append `Message.from_tool_results(results)` to history
8. Repeat from step 2

### Max iterations

When `max_iterations` is reached, force a final `complete()` call with
`tools=None`. This strips the tool option from the model, forcing a text response.
The result is degraded but coherent — the model summarises what it found so far
rather than returning a half-formed mid-tool-call response.

### Tool dispatch

Tools are indexed by name at construction: `dict[str, BoundTool]`. If the model
calls an unknown tool name, the agent returns a `ToolResult(is_error=True)` with
a descriptive message rather than raising.

---

## 7. Workflow (`workflow.py`)

A generic async pipeline engine. **Not Argus-specific** — it can execute any
directed graph of async nodes.

### Core types

```python
class Node(ABC):
    @abstractmethod
    async def execute(self, input: Any) -> Any: ...

class Edge(ABC):
    @abstractmethod
    async def route(self, output: Any) -> list[tuple[Node, Any]]: ...

@dataclass
class Workflow:
    """Single-use. Create a new instance per run() call.
    Stateful edges (JoinEdge) are not safe to reuse across concurrent runs."""
    entry: Node
    edges: dict[Node, Edge] = field(default_factory=dict)

    async def run(self, initial_input: Any) -> Any: ...
```

### Execution model

`_dispatch(node, input)` is the internal recursive/iterative runner:

- **Terminal node** (no edge): return the node's output
- **Single next pair**: tail-call optimised via `while True` loop — avoids stack
  growth in long sequential chains
- **Multiple next pairs**: fan-out via `asyncio.create_task()` + `asyncio.gather()`.
  Each task inherits a copy of the current `ContextVar` snapshot, so
  `SessionContext` (defined in `agent_core`) propagates into branches automatically
  without explicit wiring
- **Empty list from edge**: branch terminates silently (used by `JoinEdge` to gate
  on unfinished collection)

`run()` returns whatever `_dispatch` returns — terminal output for sequential
workflows, `list[Any]` for fan-out (may contain `None` from branches that
terminated at a join gate).

### Built-in edge types

| Edge | Behaviour |
|---|---|
| `DirectedEdge(target)` | Always routes to one node with the same input |
| `FanOutEdge(*targets)` | Routes to N nodes, same output as input to each |
| `ConditionalEdge(fn)` | Calls `fn(output) -> list[tuple[Node, Any]]` to decide |
| `JoinEdge(expected, target, fn)` | Collects N inputs, fires `fn(inputs)` to target |

### JoinEdge — concurrency notes

`JoinEdge` is stateful: it accumulates inputs across branches in `_collected`.

**Within a single run:** safe in asyncio without a lock. `route()` contains no
`await`, so the event loop never yields mid-collection. Single-threaded cooperative
scheduling is the invariant.

**Across concurrent runs:** `Workflow` is single-use. The dispatcher in
`github_connect` constructs a new `Workflow` (and therefore new `JoinEdge`
instances) for each incoming event. Reusing a `Workflow` instance across concurrent
`run()` calls would mix inputs from different runs.

---

## Key Decisions

| # | Decision | Rationale |
|---|---|---|
| W01 | Typed `ContentBlock` union, not `list[dict]` | Avoids leaking provider wire format into domain model |
| W02 | `cache: bool` on `TextBlock` | Provider-neutral intent; only Anthropic acts on it today |
| W03 | `BoundTool` merges schema + `process()` | No use case for schema-only tool objects; subclass pattern supports injected deps |
| W04 | `system` as top-level `complete()` param | Conceptually distinct from conversation history in every provider |
| W05 | `RetryClient` wrapper, not per-provider retry | DRY; independently testable; automatic via factory |
| W06 | `MockLLMClient` uses response queue + `self.calls` | Catches over-calling; enables assertion on sent messages |
| W07 | `Agent` is concrete, not a base class | Composition over inheritance; system prompt and domain logic stay in `agent_core` nodes |
| W08 | Agent history is local to `run()` | Agent is stateless across calls; caller owns context construction |
| W09 | Max iterations → forced final `complete(tools=None)` | Degraded-but-coherent output over truncated mid-tool response |
| W10 | `Workflow` is single-use | `JoinEdge` state is per-instance; reuse across runs causes input collision |
| W11 | Fan-out via `create_task` not `gather` directly | `create_task` copies `ContextVar` snapshot; branches inherit session context |
| W12 | Sequential dispatch via `while True` loop | Avoids stack growth for long chains or agent sub-loops |
| W13 | No streaming in `LLMClient` now | Argus posts finished comments; no UI needs token-by-token output today |
