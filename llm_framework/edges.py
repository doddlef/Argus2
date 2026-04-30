from __future__ import annotations

from typing import Any, Callable

from .workflow import Edge, Node


class DirectedEdge(Edge):
    """Always routes to a single target node with the same input."""

    def __init__(self, target: Node) -> None:
        self._target = target

    async def route(self, output: Any) -> list[tuple[Node, Any]]:
        return [(self._target, output)]


class FanOutEdge(Edge):
    """Routes to N target nodes, passing the same output as input to each."""

    def __init__(self, *targets: Node) -> None:
        self._targets = targets

    async def route(self, output: Any) -> list[tuple[Node, Any]]:
        return [(target, output) for target in self._targets]


class ConditionalEdge(Edge):
    """Delegates routing to a caller-supplied function.

    fn receives the node output and returns the list of (next_node, next_input)
    pairs, giving full control over branching and input transformation.
    """

    def __init__(self, fn: Callable[[Any], list[tuple[Node, Any]]]) -> None:
        self._fn = fn

    async def route(self, output: Any) -> list[tuple[Node, Any]]:
        return self._fn(output)


class JoinEdge(Edge):
    """Collects N inputs from concurrent branches, then fires fn(inputs) to target.

    Safety guarantees:
    - Within a single run: route() contains no await, so the event loop never
      yields mid-collection. Single-threaded cooperative scheduling makes this
      safe without a lock.
    - Across concurrent runs: not safe — use a fresh Workflow instance per run (W10).
    """

    def __init__(
        self,
        expected: int,
        target: Node,
        fn: Callable[[list[Any]], Any],
    ) -> None:
        self._expected = expected
        self._target = target
        self._fn = fn
        self._collected: list[Any] = []

    async def route(self, output: Any) -> list[tuple[Node, Any]]:
        self._collected.append(output)
        if len(self._collected) < self._expected:
            return []  # not yet satisfied — branch terminates silently
        return [(self._target, self._fn(self._collected))]
