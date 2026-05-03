from __future__ import annotations

import json
import uuid
from typing import Any

from ..client import LLMClient
from ..messages import Message, TextBlock, ToolResultBlock, ToolUseBlock
from ..responses import CompleteOptions, LLMResponse
from ..tools import BoundTool, ToolCall


class OpenRouterClient(LLMClient):
    """LLMClient backed by the OpenRouter OpenAI-compatible chat API."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        app_name: str | None = None,
        app_url: str | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError("openai package required: pip install openai")
        default_headers: dict[str, str] = {}
        if app_url:
            default_headers["HTTP-Referer"] = app_url
        if app_name:
            default_headers["X-Title"] = app_name
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers or None,
        )
        self._model = model

    async def complete(
        self,
        messages: list[Message],
        tools: list[BoundTool] | None = None,
        system: str | None = None,
        options: CompleteOptions | None = None,
    ) -> LLMResponse:
        opts = options or CompleteOptions()
        encoded = self._encode_messages(messages=messages, system=system)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": encoded,
            "max_tokens": opts.max_tokens,
        }
        if opts.temperature is not None:
            kwargs["temperature"] = opts.temperature
        if tools:
            kwargs["tools"] = [self._encode_tool(t) for t in tools]
            kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)
        return self._decode_response(response)

    def _encode_messages(self, messages: list[Message], system: str | None) -> list[dict[str, Any]]:
        encoded: list[dict[str, Any]] = []
        if system is not None:
            encoded.append({"role": "system", "content": system})
        for message in messages:
            encoded.extend(self._encode_message(message))
        return encoded

    def _encode_message(self, message: Message) -> list[dict[str, Any]]:
        if isinstance(message.content, str):
            return [{"role": message.role, "content": message.content}]

        tool_results = [b for b in message.content if isinstance(b, ToolResultBlock)]
        other_blocks = [b for b in message.content if not isinstance(b, ToolResultBlock)]
        out: list[dict[str, Any]] = []

        if other_blocks:
            text = " ".join(b.text for b in other_blocks if isinstance(b, TextBlock))
            tool_calls = [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {"name": b.name, "arguments": json.dumps(b.input)},
                }
                for b in other_blocks
                if isinstance(b, ToolUseBlock)
            ]
            msg: dict[str, Any] = {"role": message.role, "content": text}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)

        for tr in tool_results:
            out.append({
                "role": "tool",
                "tool_call_id": tr.tool_use_id,
                "content": tr.content,
            })
        return out

    def _encode_tool(self, tool: BoundTool) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.schema,
            },
        }

    def _decode_response(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        msg = choice.message
        content = msg.content or None

        tool_calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            raw_args = tc.function.arguments if tc.function else "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(
                id=tc.id or str(uuid.uuid4()),
                name=tc.function.name,
                arguments=args,
            ))

        finish_reason = choice.finish_reason
        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"

        return LLMResponse(content=content, tool_calls=tool_calls, stop_reason=stop_reason)
