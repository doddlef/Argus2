from __future__ import annotations

from collections import deque

from ..client import LLMClient
from ..messages import Message
from ..responses import CompleteOptions, LLMResponse
from ..tools import BoundTool


class MockLLMClient(LLMClient):
    """Scriptable test double for LLMClient.

    Pre-load a sequence of LLMResponse objects. Each complete() call dequeues
    the next response. Raises AssertionError if complete() is called more times
    than responses were provided — this catches over-calling in tests rather than
    silently returning a default.

    self.calls records every messages list received, in order, so tests can assert
    on what the agent actually sent to the model.
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._queue: deque[LLMResponse] = deque(responses)
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[BoundTool] | None = None,
        system: str | None = None,
        options: CompleteOptions | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))  # snapshot so mutations don't affect the record
        if not self._queue:
            raise AssertionError(
                f"MockLLMClient exhausted after {len(self.calls)} call(s) — "
                "add more responses or check for unexpected complete() calls"
            )
        return self._queue.popleft()
