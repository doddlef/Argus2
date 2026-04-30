from __future__ import annotations

import uuid
from typing import Any

from ..client import LLMClient
from ..messages import Message, TextBlock, ToolResultBlock, ToolUseBlock
from ..responses import CompleteOptions, LLMResponse
from ..tools import BoundTool, ToolCall


class OllamaClient(LLMClient):
    """LLMClient backed by the Ollama HTTP API.

    The ollama package is imported lazily inside __init__ — the module can be
    imported without the SDK installed; the error is raised only at instantiation.

    Wire format differences from Anthropic that this class handles:
    - system is prepended as a {"role": "system"} message (no top-level field)
    - tool results use role "tool" instead of a user-role ToolResultBlock
    - tool call IDs are not provided by Ollama; we generate UUIDs locally
    - cache fields on TextBlock are silently ignored
    """

    def __init__(self, model: str, host: str = "http://localhost:11434") -> None:
        try:
            import ollama
            self._client = ollama.AsyncClient(host=host)
        except ImportError:
            raise ImportError("ollama package required: pip install ollama")
        self._model = model

    async def complete(
        self,
        messages: list[Message],
        tools: list[BoundTool] | None = None,
        system: str | None = None,
        options: CompleteOptions | None = None,
    ) -> LLMResponse:
        opts = options or CompleteOptions()

        encoded: list[dict[str, Any]] = []
        if system is not None:
            encoded.append({"role": "system", "content": system})
        for message in messages:
            encoded.extend(self._encode_message(message))

        ollama_options: dict[str, Any] = {"num_predict": opts.max_tokens}
        if opts.temperature is not None:
            ollama_options["temperature"] = opts.temperature

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": encoded,
            "options": ollama_options,
        }
        if tools:
            kwargs["tools"] = [self._encode_tool(t) for t in tools]

        response = await self._client.chat(**kwargs)
        return self._decode_response(response)

    # ------------------------------------------------------------------
    # Encoding: our types → Ollama wire format
    # ------------------------------------------------------------------

    def _encode_message(self, message: Message) -> list[dict[str, Any]]:
        """One Message may expand into multiple Ollama messages.

        Ollama uses role "tool" for tool results rather than embedding them
        as blocks inside a user message, so each ToolResultBlock becomes its
        own message in the output list.
        """
        if isinstance(message.content, str):
            return [{"role": message.role, "content": message.content}]

        tool_results = [b for b in message.content if isinstance(b, ToolResultBlock)]
        other_blocks = [b for b in message.content if not isinstance(b, ToolResultBlock)]

        result: list[dict[str, Any]] = []

        if other_blocks:
            text = " ".join(b.text for b in other_blocks if isinstance(b, TextBlock))
            tool_calls = [
                {"function": {"name": b.name, "arguments": b.input}}
                for b in other_blocks
                if isinstance(b, ToolUseBlock)
            ]
            msg: dict[str, Any] = {"role": message.role, "content": text}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            result.append(msg)

        for tr in tool_results:
            result.append({"role": "tool", "content": tr.content})

        return result

    def _encode_tool(self, tool: BoundTool) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.schema,
            },
        }

    # ------------------------------------------------------------------
    # Decoding: Ollama response → our LLMResponse
    # ------------------------------------------------------------------

    def _decode_response(self, response: Any) -> LLMResponse:
        message = response.message
        text: str | None = message.content or None

        tool_calls: list[ToolCall] = []
        for tc in message.tool_calls or []:
            tool_calls.append(ToolCall(
                id=str(uuid.uuid4()),  # Ollama provides no call ID; generate one
                name=tc.function.name,
                arguments=tc.function.arguments,
            ))

        done_reason = getattr(response, "done_reason", None) or "stop"
        stop_reason_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
        stop_reason = stop_reason_map.get(done_reason, "end_turn")

        return LLMResponse(content=text, tool_calls=tool_calls, stop_reason=stop_reason)
