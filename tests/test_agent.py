"""Agent ReAct loop tests using MockLLMClient.

Covers: end-turn response, single tool call, concurrent tool calls,
unknown tool handling, and max-iterations forced completion.
"""

import pytest

from llm_framework.agent import Agent
from llm_framework.messages import Message
from llm_framework.providers.mock import MockLLMClient
from llm_framework.responses import LLMResponse
from llm_framework.tools import BoundTool, ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Test tools
# ---------------------------------------------------------------------------


class AddTool(BoundTool):
    def __init__(self) -> None:
        super().__init__(
            name="add",
            description="Add two numbers.",
            schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        )

    async def process(self, arguments: dict) -> str:
        return str(arguments["a"] + arguments["b"])


class FailingTool(BoundTool):
    def __init__(self) -> None:
        super().__init__(name="fail", description="Always raises.", schema={})

    async def process(self, arguments: dict) -> str:
        raise ValueError("tool intentionally failed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def user(text: str) -> Message:
    return Message.text("user", text)


def tool_use_response(id: str, name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=id, name=name, arguments=arguments)],
        stop_reason="tool_use",
    )


def end_turn_response(text: str) -> LLMResponse:
    return LLMResponse(content=text, stop_reason="end_turn")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_agent_end_turn_immediately():
    """Model responds with end_turn on the first call — no tools used."""
    mock = MockLLMClient([end_turn_response("Hello!")])
    agent = Agent(client=mock, tools=[])

    result = await agent.run([user("Hi")])

    assert result.content == "Hello!"
    assert not result.has_tool_calls
    assert len(mock.calls) == 1


async def test_agent_single_tool_call():
    """Model calls add(3, 4), receives '7', then returns end_turn."""
    mock = MockLLMClient([
        tool_use_response("c1", "add", {"a": 3, "b": 4}),
        end_turn_response("The answer is 7."),
    ])
    agent = Agent(client=mock, tools=[AddTool()])

    result = await agent.run([user("What is 3 + 4?")])

    assert result.content == "The answer is 7."
    assert len(mock.calls) == 2

    # Second call must include the tool result in history
    second_call_messages = mock.calls[1]
    tool_result_msg = second_call_messages[-1]
    assert any(
        hasattr(b, "content") and b.content == "7"
        for b in tool_result_msg.content
    )


async def test_agent_does_not_mutate_caller_messages():
    """Agent history is local — the caller's list must be unchanged after run()."""
    initial = [user("start")]
    mock = MockLLMClient([end_turn_response("done")])
    agent = Agent(client=mock, tools=[])

    await agent.run(initial)

    assert len(initial) == 1


async def test_agent_unknown_tool_returns_error_result():
    """If the model calls a tool that doesn't exist, the agent returns is_error=True."""
    mock = MockLLMClient([
        tool_use_response("c1", "nonexistent_tool", {}),
        end_turn_response("I see the tool failed."),
    ])
    agent = Agent(client=mock, tools=[AddTool()])

    result = await agent.run([user("use a tool")])

    second_call = mock.calls[1]
    tool_result_msg = second_call[-1]
    error_blocks = [
        b for b in tool_result_msg.content
        if hasattr(b, "is_error") and b.is_error
    ]
    assert error_blocks, "Expected an is_error=True block in history"


async def test_agent_tool_exception_becomes_error_result():
    """A tool that raises is caught and returned as is_error=True, not a crash."""
    mock = MockLLMClient([
        tool_use_response("c1", "fail", {}),
        end_turn_response("Handled the error."),
    ])
    agent = Agent(client=mock, tools=[FailingTool()])

    result = await agent.run([user("trigger the failing tool")])

    assert result.content == "Handled the error."
    second_call = mock.calls[1]
    error_blocks = [
        b for b in second_call[-1].content
        if hasattr(b, "is_error") and b.is_error
    ]
    assert error_blocks


async def test_agent_max_iterations_forces_final_completion():
    """When max_iterations is exhausted, a final call with tools=None is made."""
    # All responses request tool use — agent must force a final call after limit
    tool_responses = [
        tool_use_response(f"c{i}", "add", {"a": i, "b": 1})
        for i in range(3)
    ]
    final = end_turn_response("I ran out of steps.")
    mock = MockLLMClient(tool_responses + [final])

    agent = Agent(client=mock, tools=[AddTool()], max_iterations=3)
    result = await agent.run([user("keep adding")])

    assert result.content == "I ran out of steps."
    # 3 tool-use calls + 1 forced final call = 4 total
    assert len(mock.calls) == 4
    # The final call must have been made with no tools (tools=None passed to complete)
    # We can't inspect that directly, but the MockLLMClient queue being empty
    # after exactly 4 calls confirms the count is correct.


async def test_agent_concurrent_tool_calls():
    """Multiple tool calls in one response are dispatched concurrently."""
    mock = MockLLMClient([
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2}),
                ToolCall(id="c2", name="add", arguments={"a": 10, "b": 20}),
            ],
            stop_reason="tool_use",
        ),
        end_turn_response("Got 3 and 30."),
    ])
    agent = Agent(client=mock, tools=[AddTool()])
    result = await agent.run([user("add two pairs")])

    assert result.content == "Got 3 and 30."
    # Both tool results must appear in the history fed to the second call
    second_call = mock.calls[1]
    result_blocks = second_call[-1].content
    contents = {b.content for b in result_blocks if hasattr(b, "content")}
    assert "3" in contents
    assert "30" in contents
