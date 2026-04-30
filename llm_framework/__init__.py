"""llm_framework — model-agnostic LLM capability layer.

Public API. Upper layers (agent_core, github_connect) import from here.
Provider clients (AnthropicClient, OllamaClient) are in llm_framework.providers
and have optional SDK dependencies — import them directly when needed.
"""

from .agent import Agent
from .client import LLMClient, RetryClient
from .edges import ConditionalEdge, DirectedEdge, FanOutEdge, JoinEdge
from .messages import ContentBlock, Message, TextBlock, ToolResultBlock, ToolUseBlock
from .responses import CompleteOptions, LLMResponse
from .tools import BoundTool, ToolCall, ToolResult
from .workflow import Edge, Node, Workflow

__all__ = [
    # Messages
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ContentBlock",
    "Message",
    # Tools
    "BoundTool",
    "ToolCall",
    "ToolResult",
    # Responses
    "CompleteOptions",
    "LLMResponse",
    # Client
    "LLMClient",
    "RetryClient",
    # Agent
    "Agent",
    # Workflow
    "Node",
    "Edge",
    "Workflow",
    # Edges
    "DirectedEdge",
    "FanOutEdge",
    "ConditionalEdge",
    "JoinEdge",
]
