from __future__ import annotations

import asyncio
import logging

from .client import LLMClient
from .messages import Message
from .responses import CompleteOptions, LLMResponse
from .tools import BoundTool, ToolCall, ToolResult

logger = logging.getLogger(__name__)


class Agent:
    """Concrete ReAct loop executor. Not a base class — compose into nodes (W07).

    System prompts and domain logic live in the caller (agent_core nodes).
    History is local to each run() call — stateless across calls (W08).
    """

    def __init__(
        self,
        client: LLMClient,
        tools: list[BoundTool],
        system: str | None = None,
        max_iterations: int = 10,
        options: CompleteOptions | None = None,
    ) -> None:
        self._client = client
        self._tool_index: dict[str, BoundTool] = {t.name: t for t in tools}
        self._system = system
        self._max_iterations = max_iterations
        self._options = options

    async def run(self, messages: list[Message]) -> LLMResponse:
        history = list(messages)  # local copy — caller's list is never mutated (W08)
        tools = list(self._tool_index.values())

        for _ in range(self._max_iterations):
            response = await self._client.complete(
                history, tools, self._system, self._options
            )

            if not response.has_tool_calls:
                return response

            history.append(response.to_message())

            results: list[ToolResult] = list(
                await asyncio.gather(*[self._call_tool(c) for c in response.tool_calls])
            )
            history.append(Message.from_tool_results(results))

        # Exhausted iterations — strip tools to force a coherent text response
        # rather than returning a mid-loop state (W09)
        logger.warning(
            "Agent reached max_iterations=%d; forcing final completion without tools",
            self._max_iterations,
        )
        return await self._client.complete(
            history, tools=None, system=self._system, options=self._options
        )

    async def _call_tool(self, call: ToolCall) -> ToolResult:
        tool = self._tool_index.get(call.name)
        if tool is None:
            return ToolResult(
                id=call.id,
                content=f"Unknown tool {call.name!r}. Available: {list(self._tool_index)!r}",
                is_error=True,
            )
        try:
            content = await tool.process(call.arguments)
            return ToolResult(id=call.id, content=content)
        except Exception as exc:
            logger.warning("Tool %r raised: %s", call.name, exc)
            return ToolResult(id=call.id, content=str(exc), is_error=True)
