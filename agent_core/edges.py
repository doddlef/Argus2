"""Domain-aware edges for PipelineState[T] fan-out and join.

These complement llm_framework's generic edges. They understand PipelineState's
fan_n/fan_i coordination protocol so llm_framework can remain state-agnostic.
"""

from __future__ import annotations

from typing import Any

from llm_framework.workflow import Edge, Node

from .context import PipelinePayload, PipelineState


class PipelineStateFanOutEdge(Edge):
    """Fans state.data (a list of T) into N PipelineState[item] branches.

    All branches are routed to the same target node. Each branch receives a copy
    of the payload reference (safe: Analysis nodes only read payload after fan-out)
    and fan_n/fan_i set for the downstream JoinEdge.

    Caller is responsible for the 0-item case: if state.data is empty, route()
    returns an empty list and the pipeline terminates silently. Use a ConditionalEdge
    before this one to bypass to the join target when the list is empty.
    """

    def __init__(self, target: Node) -> None:
        self._target = target

    async def route(self, output: PipelineState) -> list[tuple[Node, Any]]:
        items = output.data
        n = len(items)
        return [
            (
                self._target,
                PipelineState(
                    payload=output.payload,
                    data=item,
                    fan_n=n,
                    fan_i=i,
                ),
            )
            for i, item in enumerate(items)
        ]


class PipelineStateJoinEdge(Edge):
    """Collects N PipelineState branches and merges them into PipelineState[list[T]].

    Expected branch count is read dynamically from the first arriving state's fan_n.
    Branches are ordered by fan_i before merging. fan_n and fan_i are cleared in
    the output state.

    Thread-safety: route() contains no awaits, so cooperative scheduling prevents
    interleaving within a single run. Not safe across concurrent runs — use a fresh
    Workflow instance per event (same constraint as llm_framework.JoinEdge).
    """

    def __init__(self, target: Node) -> None:
        self._target = target
        self._collected: list[PipelineState] = []
        self._expected: int | None = None

    async def route(self, output: PipelineState) -> list[tuple[Node, Any]]:
        if self._expected is None:
            self._expected = output.fan_n
        self._collected.append(output)
        if len(self._collected) < self._expected:
            return []
        ordered = sorted(self._collected, key=lambda s: s.fan_i)
        return [
            (
                self._target,
                PipelineState(
                    payload=ordered[0].payload,
                    data=[s.data for s in ordered],
                    fan_n=None,
                    fan_i=None,
                ),
            )
        ]
