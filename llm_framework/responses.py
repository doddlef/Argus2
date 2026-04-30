from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .messages import ContentBlock, Message, TextBlock, ToolUseBlock
from .tools import ToolCall


@dataclass
class CompleteOptions:
    """Per-call overrides for model behavior."""

    max_tokens: int = 8096
    temperature: float | None = None


@dataclass
class LLMResponse:
    content: str | None   # None when the model responds with only tool calls
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: Literal["end_turn", "tool_use", "max_tokens"] = "end_turn"

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_message(self) -> Message:
        """Convert this response into an assistant Message for history reconstruction.

        Symmetric counterpart to Message.from_tool_results().
        Uses the str shorthand when there is only text and no tool calls.
        """
        if not self.tool_calls:
            # str shorthand is sufficient — no tool blocks needed
            return Message(role="assistant", content=self.content or "")

        blocks: list[ContentBlock] = []
        if self.content is not None:
            blocks.append(TextBlock(text=self.content))
        for call in self.tool_calls:
            blocks.append(ToolUseBlock(id=call.id, name=call.name, input=call.arguments))
        return Message(role="assistant", content=blocks)
