from __future__ import annotations

from typing import Any

from ..client import LLMClient
from ..messages import ContentBlock, Message, TextBlock, ToolResultBlock, ToolUseBlock
from ..responses import CompleteOptions, LLMResponse
from ..tools import BoundTool, ToolCall


class AnthropicClient(LLMClient):
    """LLMClient backed by the Anthropic SDK.

    The anthropic package is imported lazily inside __init__ — the module can be
    imported without the SDK installed; the error is raised only at instantiation.
    """

    def __init__(self, model: str, api_key: str | None = None) -> None:
        try:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        except ImportError:
            raise ImportError("anthropic package required: pip install anthropic")
        self._model = model

    async def complete(
        self,
        messages: list[Message],
        tools: list[BoundTool] | None = None,
        system: str | None = None,
        options: CompleteOptions | None = None,
    ) -> LLMResponse:
        opts = options or CompleteOptions()

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": opts.max_tokens,
            "messages": [self._encode_message(m) for m in messages],
        }
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [self._encode_tool(t) for t in tools]
        if opts.temperature is not None:
            kwargs["temperature"] = opts.temperature

        response = await self._client.messages.create(**kwargs)
        return self._decode_response(response)

    # ------------------------------------------------------------------
    # Encoding: our types → Anthropic wire format
    # ------------------------------------------------------------------

    def _encode_message(self, message: Message) -> dict[str, Any]:
        if isinstance(message.content, str):
            return {"role": message.role, "content": message.content}
        return {
            "role": message.role,
            "content": [self._encode_block(b) for b in message.content],
        }

    def _encode_block(self, block: ContentBlock) -> dict[str, Any]:
        if isinstance(block, TextBlock):
            encoded: dict[str, Any] = {"type": "text", "text": block.text}
            if block.cache:
                encoded["cache_control"] = {"type": "ephemeral"}
            return encoded
        if isinstance(block, ToolUseBlock):
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        if isinstance(block, ToolResultBlock):
            return {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
                "is_error": block.is_error,
            }
        raise ValueError(f"Unknown ContentBlock type: {type(block)}")

    def _encode_tool(self, tool: BoundTool) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.schema,   # Anthropic uses input_schema, not schema
        }

    # ------------------------------------------------------------------
    # Decoding: Anthropic response → our LLMResponse
    # ------------------------------------------------------------------

    def _decode_response(self, response: Any) -> LLMResponse:
        text: str | None = None
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text = block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))

        raw_stop = response.stop_reason or "end_turn"
        # Anthropic may return "stop_sequence" or other values not in our set
        if raw_stop not in ("end_turn", "tool_use", "max_tokens"):
            raw_stop = "end_turn"

        return LLMResponse(content=text, tool_calls=tool_calls, stop_reason=raw_stop)
