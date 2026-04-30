from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .tools import ToolResult


@dataclass
class TextBlock:
    text: str
    cache: bool = False   # maps to cache_control: {"type": "ephemeral"} in AnthropicClient


@dataclass
class ToolUseBlock:
    """Represents a tool call made by the model inside an assistant message."""

    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    """Carries a tool result back to the model in a user message."""

    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: str | list[ContentBlock]

    @staticmethod
    def text(role: Literal["user", "assistant"], text: str) -> Message:
        """Convenience constructor for plain text messages."""
        return Message(role=role, content=text)

    @staticmethod
    def from_tool_results(results: list[ToolResult]) -> Message:
        """Build the user-role message that feeds tool results back to the model.

        Takes list[ToolResult] (not list[str]) so is_error is preserved.
        ToolResult.id already carries the matching call ID; no ToolCall list needed.
        """
        return Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=r.id,
                    content=r.content,
                    is_error=r.is_error,
                )
                for r in results
            ],
        )
