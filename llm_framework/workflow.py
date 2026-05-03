from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class Node(ABC):
    @abstractmethod
    async def execute(self, input: Any) -> Any: ...


class Edge(ABC):
    @abstractmethod
    async def route(self, output: Any) -> list[tuple[Node, Any]]: ...


@dataclass
class Workflow:
    """Single-use per run() call.

    Stateful edges (JoinEdge) accumulate inputs in instance state — reusing a
    Workflow across concurrent run() calls mixes inputs from different runs (W10).
    The dispatcher in github_connect constructs a fresh Workflow per incoming event.
    """

    entry: Node
    edges: dict[Node, Edge] = field(default_factory=dict)

    async def run(self, initial_input: Any) -> Any:
        return await self._dispatch(self.entry, initial_input)

    async def _dispatch(self, node: Node, input: Any) -> Any:
        while True:
            logger.info("workflow node start node=%s", type(node).__name__)
            output = await node.execute(input)
            logger.info("workflow node done node=%s", type(node).__name__)

            edge = self.edges.get(node)
            if edge is None:
                logger.info("workflow terminal node=%s", type(node).__name__)
                return output  # terminal node

            next_pairs = await edge.route(output)
            logger.info(
                "workflow route edge=%s from=%s next_count=%d",
                type(edge).__name__,
                type(node).__name__,
                len(next_pairs),
            )

            if not next_pairs:
                # Branch terminated silently — used by JoinEdge to gate on
                # unfinished collection without raising or returning early
                return None

            if len(next_pairs) == 1:
                # Tail-call optimization: reuse the current stack frame instead
                # of recursing, so long sequential chains don't grow the stack (W12)
                node, input = next_pairs[0]
                continue

            # Fan-out: create_task copies the current ContextVar snapshot into each
            # branch, so SessionContext propagates without explicit wiring (W11)
            tasks = [
                asyncio.create_task(self._dispatch(n, i))
                for n, i in next_pairs
            ]
            logger.info("workflow fanout branches=%d from=%s", len(tasks), type(node).__name__)
            return await asyncio.gather(*tasks)
