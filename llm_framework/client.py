from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from .messages import Message
from .responses import CompleteOptions, LLMResponse
from .tools import BoundTool

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[BoundTool] | None = None,
        system: str | None = None,
        options: CompleteOptions | None = None,
    ) -> LLMResponse:
        """Send a completion request to the underlying model.

        system is a top-level param (not embedded in messages) because every
        provider conceptually supports a system prompt. Providers that lack a
        native system field prepend it as a message internally.
        """
        ...


class RetryClient(LLMClient):
    """Wraps any LLMClient and retries on transient errors with exponential backoff.

    Retry logic lives here rather than inside individual provider clients so it
    is testable independently and not duplicated across providers (W05).
    ApplicationContext wraps every provider client in RetryClient automatically.
    """

    def __init__(self, inner: LLMClient, max_retries: int = 3) -> None:
        self._inner = inner
        self._max_retries = max_retries

    async def complete(
        self,
        messages: list[Message],
        tools: list[BoundTool] | None = None,
        system: str | None = None,
        options: CompleteOptions | None = None,
    ) -> LLMResponse:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._inner.complete(messages, tools, system, options)
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(
                        "LLM call failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1,
                        self._max_retries,
                        wait,
                        exc,
                    )
                    await asyncio.sleep(wait)

        assert last_exc is not None
        raise last_exc
