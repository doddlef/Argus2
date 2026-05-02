"""Conversation-phase node.

ConversationNode is used in the comment pipeline's 'conversation' branch.
It runs a ReAct loop to answer a developer's question or comment, posts the reply,
and may write contributor context to users/ wiki pages.

Failure policy: log exception + post a brief error comment (non-fatal to the caller).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from llm_framework import BoundTool, LLMClient, Message, ToolResult
from llm_framework.tools import ToolCall
from llm_framework.workflow import Node

from ..context import PipelineState, get_session
from ..tools.code import make_code_tools
from ..tools.memory import make_memory_tools

logger = logging.getLogger(__name__)

_MAX_CONVERSATION_ITERATIONS = 10

_CONVERSATION_SYSTEM = """\
You are a Conversation agent for Argus, a code reviewer. Your task is to respond to a
developer's question or comment on a Pull Request.

Workflow:
1. Read the PR context, thread history, and the triggering comment carefully.
2. Use code tools (list, read_diff, read_file, search) to look up anything you need.
3. Consult the wiki (memory_read) for relevant codebase context before answering.
4. If you discover non-obvious context about a contributor (e.g. they own module X, have a
   known preference), write it to users/{username}.md via memory_write or memory_append.
5. When you have gathered enough to respond, call submit_reply with your complete Markdown
   response. This is your FINAL action — make no tool calls after it.

Keep responses developer-friendly: precise, concise, and actionable.\
"""


# ---------------------------------------------------------------------------
# Capture tool
# ---------------------------------------------------------------------------

class _SubmitReplyTool(BoundTool):
    def __init__(self) -> None:
        super().__init__(
            name="submit_reply",
            description=(
                "Submit the final reply to post as a PR comment. "
                "Call this as your final action."
            ),
            schema={
                "type": "object",
                "properties": {
                    "body": {
                        "type": "string",
                        "description": "Markdown comment body for the PR reply.",
                    }
                },
                "required": ["body"],
            },
        )
        self.captured: str | None = None

    async def process(self, arguments: dict) -> str:
        self.captured = arguments.get("body", "")
        return "Reply submitted. Do not make any more tool calls."


# ---------------------------------------------------------------------------
# ConversationNode
# ---------------------------------------------------------------------------

class ConversationNode(Node):
    """Answers a PR comment via a ReAct loop and posts the reply.

    comment_body is provided at construction time by the pipeline builder
    (it comes from CommentReceivedTrigger and is not carried in PipelineState).
    """

    def __init__(
        self,
        standard_client: LLMClient,
        wiki_root: Path,
        max_file_window_lines: int,
        max_search_results: int,
        comment_body: str,
    ) -> None:
        self._client = standard_client
        self._wiki_root = wiki_root
        self._max_file_window_lines = max_file_window_lines
        self._max_search_results = max_search_results
        self._comment_body = comment_body

    async def execute(self, state: PipelineState[Any]) -> None:
        ctx = get_session()
        submit_reply = _SubmitReplyTool()

        code_tools = make_code_tools(
            state.payload.file_tree,
            self._max_file_window_lines,
            self._max_search_results,
        )
        memory_tools = make_memory_tools(self._wiki_root)
        all_tools = code_tools + memory_tools + [submit_reply]
        tool_index = {t.name: t for t in all_tools}

        initial_msg = _build_conversation_message(ctx, state.payload, self._comment_body)
        history = [Message.text("user", initial_msg)]

        reply: str | None = None
        try:
            for _ in range(_MAX_CONVERSATION_ITERATIONS):
                response = await self._client.complete(
                    messages=history,
                    tools=all_tools,
                    system=_CONVERSATION_SYSTEM,
                )

                if not response.tool_calls:
                    break

                history.append(response.to_message())
                results = list(
                    await asyncio.gather(*[
                        _dispatch_tool(tool_index, call)
                        for call in response.tool_calls
                    ])
                )
                history.append(Message.from_tool_results(results))

                if submit_reply.captured is not None:
                    break

            reply = submit_reply.captured or response.content or None

        except Exception as exc:
            logger.exception("Conversation ReAct loop failed: %s", exc)

        if reply:
            try:
                await ctx.commenter.post_comment(ctx.pr_number, reply)
            except Exception as exc:
                logger.exception("Conversation post_comment failed: %s", exc)
        else:
            try:
                await ctx.commenter.post_comment(
                    ctx.pr_number,
                    "Argus encountered an error while processing your comment.",
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_conversation_message(ctx: Any, payload: Any, comment_body: str) -> str:
    sections = [
        f"## PR #{ctx.pr_number}: {ctx.metadata.title}",
        f"**Author:** {ctx.metadata.author}  |  **Branch:** {ctx.metadata.head_branch}",
        "",
        "## Triggering Comment",
        comment_body,
        "",
    ]

    if payload.thread_summary:
        sections += ["## Thread Summary", payload.thread_summary, ""]
    if payload.recent_comments:
        recent = "\n".join(f"[{c.author}]: {c.body}" for c in payload.recent_comments)
        sections += ["## Recent Comments", recent, ""]

    if payload.wiki_index:
        sections += ["## Wiki Index", payload.wiki_index, ""]
    if payload.wiki_structure:
        sections += ["## Codebase Structure", payload.wiki_structure, ""]

    if payload.file_tree:
        sections += ["## Repository File Tree", payload.file_tree.rendered, ""]

    sections.append(
        "Use the available tools to research your response, then call submit_reply "
        "with your complete Markdown reply."
    )

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Shared ReAct dispatch helper
# ---------------------------------------------------------------------------

async def _dispatch_tool(
    tool_index: dict[str, BoundTool], call: ToolCall
) -> ToolResult:
    tool = tool_index.get(call.name)
    if tool is None:
        return ToolResult(
            id=call.id,
            content=f"Unknown tool {call.name!r}. Available: {list(tool_index)!r}",
            is_error=True,
        )
    try:
        content = await tool.process(call.arguments)
        return ToolResult(id=call.id, content=content)
    except Exception as exc:
        logger.warning("Tool %r raised: %s", call.name, exc)
        return ToolResult(id=call.id, content=str(exc), is_error=True)
