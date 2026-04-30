# llm_framework

The capability layer of Argus. **Model-agnostic** and **domain-agnostic** — it knows nothing about GitHub, pull requests, or code review.

```
github_connect → agent_core → llm_framework
```

Upper layers depend on this module. This module depends on nothing above itself.

---

## What it provides

| Area | What |
|---|---|
| **Data types** | Typed vocabulary for messages, tools, and responses |
| **LLM client** | Unified async interface over multiple providers |
| **ReAct agent** | Concrete tool-calling loop with concurrent dispatch |
| **Workflow engine** | Generic async directed-graph pipeline |

---

## Module structure

```
llm_framework/
├── messages.py        # TextBlock, ToolUseBlock, ToolResultBlock, Message
├── tools.py           # BoundTool, ToolCall, ToolResult
├── responses.py       # CompleteOptions, LLMResponse
├── client.py          # LLMClient (ABC), RetryClient
├── agent.py           # Agent — ReAct loop
├── workflow.py        # Node, Edge, Workflow
├── edges.py           # DirectedEdge, FanOutEdge, ConditionalEdge, JoinEdge
└── providers/
    ├── anthropic.py   # AnthropicClient  (requires: pip install anthropic)
    ├── ollama.py      # OllamaClient     (requires: pip install ollama)
    └── mock.py        # MockLLMClient    (test double, no extra deps)
```

---

## Quick start

### Sending a message

```python
from llm_framework import Message, CompleteOptions
from llm_framework.providers import AnthropicClient

client = AnthropicClient(model="claude-sonnet-4-6")
response = await client.complete(
    messages=[Message.text("user", "Hello!")],
    system="You are a helpful assistant.",
)
print(response.content)
```

### Defining a tool

```python
from dataclasses import dataclass
from llm_framework import BoundTool

@dataclass
class SearchTool(BoundTool):
    index: MySearchIndex          # injected dependency

    def __init__(self, index: MySearchIndex) -> None:
        super().__init__(
            name="search",
            description="Search the codebase for a symbol.",
            schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        self.index = index

    async def process(self, arguments: dict) -> str:
        return await self.index.search(arguments["query"])
```

### Running the ReAct agent

```python
from llm_framework import Agent, Message

agent = Agent(
    client=client,
    tools=[SearchTool(my_index)],
    system="You are a code reviewer.",
    max_iterations=10,
)
response = await agent.run([Message.text("user", "Review this PR.")])
print(response.content)
```

### Building a workflow

```python
from llm_framework import Node, Workflow
from llm_framework.edges import DirectedEdge, FanOutEdge, JoinEdge

class FetchNode(Node):
    async def execute(self, input):  # input = PR number
        return await fetch_pr(input)

class ReviewNode(Node):
    async def execute(self, input):  # input = PR data
        return await run_review(input)

wf = Workflow(
    entry=fetch,
    edges={fetch: DirectedEdge(review)},
)
result = await wf.run(pr_number)
```

---

## Retry and resilience

Wrap any client in `RetryClient` for automatic exponential backoff on transient errors:

```python
from llm_framework import RetryClient
from llm_framework.providers import AnthropicClient

client = RetryClient(AnthropicClient(model="claude-sonnet-4-6"), max_retries=3)
```

`ApplicationContext` in `agent_core` does this automatically — callers never interact with an unwrapped client.

---

## Testing

`MockLLMClient` is a scriptable test double:

```python
from llm_framework import Agent, Message, LLMResponse
from llm_framework.providers import MockLLMClient

mock = MockLLMClient([
    LLMResponse(content="Done.", stop_reason="end_turn"),
])
agent = Agent(client=mock, tools=[])
result = await agent.run([Message.text("user", "Go.")])

assert result.content == "Done."
assert len(mock.calls) == 1          # assert what was sent to the model
```

Run the test suite:

```bash
pip install pytest pytest-asyncio
pytest tests/
```

---

## Prompt caching (Anthropic)

Set `cache=True` on a `TextBlock` to mark it for Anthropic prompt caching. Other providers ignore the field.

```python
from llm_framework import Message, TextBlock

message = Message(role="user", content=[
    TextBlock(text=large_system_context, cache=True),
    TextBlock(text="Now review this diff."),
])
```

---

## Provider dependencies

Provider SDKs are imported lazily inside `__init__` — the module can be imported without any SDK installed. The `ImportError` is raised only when the client is instantiated.

| Provider | Install |
|---|---|
| Anthropic | `pip install anthropic` |
| Ollama | `pip install ollama` (also requires Ollama running locally) |
| Mock | no extra dependencies |

---

## Design decisions

Full rationale for every decision is in [`DESIGN.md`](DESIGN.md).
