# Argus — AI Code Review Agent

## What Is Argus?

Argus is a GitHub App that lives in your repository and works like a
senior engineering team member. It reads pull requests, reviews code,
posts comments, replies to questions, and maintains its own understanding
of the codebase over time.

The core mental model: **Claude Code, but it lives in GitHub.**

Like Claude Code, Argus has persistent memory, uses tools to navigate
code, and reasons about the specific project it is working on — not
code in general. Unlike Claude Code, it is triggered by GitHub events,
communicates through PR comments, and accumulates knowledge across every
PR it reviews.

---

## The Three-Layer Model

```
┌─────────────────────────────────────────────────┐
│  SCHEMA (user-owned, Argus reads)               │
│  ARGUS.md in the repo root                      │
│  Project intent, sensitivity level,             │
│  review instructions, contributor roster        │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  WIKI (Argus-owned, human-readable)             │
│  Lives on the server filesystem                 │
│  index.md, structure.md, log.md                 │
│  domain.md, module-*.md, users/*.md             │
│  Generated and maintained by Argus itself       │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  RAW (source of truth)                          │
│  The actual codebase, accessed via GitHub API   │
│  PRs, diffs, comments — never stored locally    │
└─────────────────────────────────────────────────┘
```

The schema layer is a contract the team writes to Argus.
The wiki is Argus's interpretation of the codebase, built and maintained
autonomously. The raw layer is never cloned — all code access goes
through the GitHub API.

--

## Module Architecture

Argus is structured in three layers with strict dependencies:

```
github_connect <-> agent_core -> llm_framework
```

### llm_framework

The capability layer. Model-agnostic. Knows nothing about Argus or
GitHub.

Provides: `LLMClient` (ABC), `Message`, `Tool`, `ToolCall`,
`ToolResult`, `LLMResponse`, `Agent` (base ReAct loop).

Implementations: `AnthropicClient`, `OllamaClient`, `MockLLMClient`.

Consider as a custom agent framework, like **LangChain** or **LangGraph**.

### agent_core

The domain layer. Platform-agnostic. Knows nothing about GitHub.

**Domain objects:** `Project`, `Branch`, `PullRequest`, `Diff`,
`ChangedFile`, `Comment`, `ReviewComment`, `WikiPage`, `Contributor`.

**Ports (interfaces defined here, implemented and provided in gitHub_connect):**
- `CodeReader` — get PR files, file content, file tree
- `Commenter` — post review, post comment, reply to comment

**Triggers (entry points):**
Trigger by event, or invoke:
- `PROpenedTrigger` — full review pipeline
- `PRUpdatedTrigger` — incremental review on new commits
- `CommentReceivedTrigger` — reply logic, selective silence

**Pipelines:**
Introduce in later sections

### github_connect

The infrastructure layer. GitHub-specific. Translates between GitHub's
world and the domain.

**Server:** FastAPI webhook endpoint, HMAC-SHA256 verification,
background task dispatch.

**Dispatcher:** routes event to the correct trigger, loads project
context from SQLite, builds adapter instances, injects into triggers.

**Translator:** converts GitHub JSON payloads to domain objects.

**Adapters (implement the ports):**
- `GitHubCodeReader` — calls GitHub REST API, caches by commit SHA
- `GitHubCommenter` — posts reviews and comments via GitHub API

**Auth:** HMAC webhook verification, GitHub App JWT generation,
installation access token management.

--- 

## Key Decisions

| #   | Decision                             | Rationale                                                                      |
|-----|--------------------------------------|--------------------------------------------------------------------------------|
| D01 | Python only                          | Unified codebase, first-class ML ecosystem                                     |
| D02 | SQLite                               | Single process, zero infrastructure, migrate later if needed                   |
| D03 | No local codebase clone              | All code via GitHub API + in-memory cache keyed by commit SHA                  |
| D04 | Wiki on server filesystem            | Simple now, blog-style display service can read same files later               |
| D05 | Hand-rolled pipeline                 | Well-defined enough not to need a framework. ABC design allows LangGraph later |
| D06 | Wiki anchored to default branch      | Feature branches borrow from target branch wiki                                |
| D07 | ReAct only for CORE entries          | Structured phases for all other tiers keeps cost predictable                   |
| D08 | Wiki reads free, file reads budgeted | Steers agent toward wiki first, builds richer wiki over time                   |
| D09 | Total failure → brief PR comment     | Transparency over silence                                                      |

--- 