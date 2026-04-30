"""Workflow engine tests.

Covers: DirectedEdge (simple chain), ConditionalEdge (branching),
FanOutEdge (parallel dispatch), and JoinEdge (N→1 collection).
"""

from typing import Any

import pytest

from llm_framework.edges import ConditionalEdge, DirectedEdge, FanOutEdge, JoinEdge
from llm_framework.workflow import Node, Workflow


# ---------------------------------------------------------------------------
# Reusable calculation nodes
# ---------------------------------------------------------------------------


class DoubleNode(Node):
    async def execute(self, input: int) -> int:
        return input * 2


class TripleNode(Node):
    async def execute(self, input: int) -> int:
        return input * 3


class SquareNode(Node):
    async def execute(self, input: int) -> int:
        return input * input


class AddNode(Node):
    def __init__(self, addend: int) -> None:
        self.addend = addend

    async def execute(self, input: int) -> int:
        return input + self.addend


class SumCollectedNode(Node):
    """Receives the merged list from JoinEdge and sums it."""

    async def execute(self, inputs: list[int]) -> int:
        return sum(inputs)


class LabelNode(Node):
    """Tags a value with a string label — used for conditional routing tests."""

    def __init__(self, label: str) -> None:
        self.label = label

    async def execute(self, input: int) -> str:
        return f"{self.label}:{input}"


# ---------------------------------------------------------------------------
# Simple chain — DirectedEdge
# ---------------------------------------------------------------------------


async def test_simple_chain_result():
    """run(5) → double(10) → add(3) → 13"""
    double = DoubleNode()
    add = AddNode(3)

    wf = Workflow(
        entry=double,
        edges={double: DirectedEdge(add)},
    )
    assert await wf.run(5) == 13


async def test_single_terminal_node():
    """A workflow with no edges returns the entry node's output directly."""
    assert await Workflow(entry=DoubleNode()).run(7) == 14


async def test_long_sequential_chain():
    """Five nodes in sequence — exercises the while-loop tail-call path."""
    nodes = [AddNode(1) for _ in range(5)]
    edges = {nodes[i]: DirectedEdge(nodes[i + 1]) for i in range(4)}
    wf = Workflow(entry=nodes[0], edges=edges)
    # run(0): +1 +1 +1 +1 +1 = 5
    assert await wf.run(0) == 5


# ---------------------------------------------------------------------------
# Conditional routing — ConditionalEdge
# ---------------------------------------------------------------------------


async def test_conditional_routes_even():
    """Even input is routed to DoubleNode; odd to TripleNode."""
    double = DoubleNode()
    triple = TripleNode()

    def route_by_parity(value: int) -> list[tuple[Node, Any]]:
        return [(double, value)] if value % 2 == 0 else [(triple, value)]

    entry = AddNode(0)  # pass-through
    wf = Workflow(
        entry=entry,
        edges={
            entry: ConditionalEdge(route_by_parity),
        },
    )
    assert await wf.run(4) == 8   # even → double
    assert await wf.run(3) == 9   # odd  → triple


async def test_conditional_different_inputs():
    """ConditionalEdge can supply a transformed input to the next node."""
    label_a = LabelNode("A")
    label_b = LabelNode("B")

    entry = AddNode(0)

    def route(value: int) -> list[tuple[Node, Any]]:
        if value > 10:
            return [(label_a, value)]
        return [(label_b, value * 2)]  # also transforms the input

    wf = Workflow(entry=entry, edges={entry: ConditionalEdge(route)})
    assert await wf.run(20) == "A:20"
    assert await wf.run(5) == "B:10"


# ---------------------------------------------------------------------------
# Fan-out — FanOutEdge
# ---------------------------------------------------------------------------


async def test_fanout_to_terminal_nodes():
    """Fan-out to two terminal nodes returns results from both branches."""
    double = DoubleNode()
    triple = TripleNode()

    entry = AddNode(0)
    wf = Workflow(entry=entry, edges={entry: FanOutEdge(double, triple)})

    result = await wf.run(4)
    # gather preserves task creation order: [double(4), triple(4)]
    assert result == [8, 12]


# ---------------------------------------------------------------------------
# Fan-out + Join — FanOutEdge + JoinEdge
# ---------------------------------------------------------------------------


async def test_fanout_join_collects_and_fires():
    """Fan-out to square + double, join collects both, sum node adds them.

    run(4):
      SquareNode  → 16
      DoubleNode  →  8
      JoinEdge collects [16, 8], fires sum([16, 8]) = 24 to SumCollectedNode
      SumCollectedNode(24) → 24
    """
    square = SquareNode()
    double = DoubleNode()
    collector = SumCollectedNode()

    join = JoinEdge(expected=2, target=collector, fn=lambda xs: xs)

    entry = AddNode(0)
    wf = Workflow(
        entry=entry,
        edges={
            entry: FanOutEdge(square, double),
            square: join,
            double: join,
        },
    )
    result = await wf.run(4)
    # One branch terminates with None (join gate not yet fired for that branch),
    # the other dispatches through SumCollectedNode and returns 24.
    assert 24 in result


async def test_join_fires_only_after_all_branches():
    """JoinEdge must not fire until all expected inputs have arrived."""
    terminal = AddNode(0)
    join = JoinEdge(expected=3, target=terminal, fn=sum)

    n1, n2, n3 = AddNode(1), AddNode(2), AddNode(3)
    entry = AddNode(0)

    wf = Workflow(
        entry=entry,
        edges={
            entry: FanOutEdge(n1, n2, n3),
            n1: join,
            n2: join,
            n3: join,
        },
    )
    # run(0): branches produce 1, 2, 3 → join fires sum([1,2,3])=6 → terminal(6)=6
    result = await wf.run(0)
    assert 6 in result
