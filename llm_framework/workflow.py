from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


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
            output = await node.execute(input)

            edge = self.edges.get(node)
            if edge is None:
                return output  # terminal node

            next_pairs = await edge.route(output)

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
            return await asyncio.gather(*tasks)
