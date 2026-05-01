# Memory / Wiki — Design Advice

Observations drawn from how Claude Code's own memory system works, applied to
Argus's wiki design.

---

## Core Insight

The index is cheap to always load. Individual pages are expensive. Therefore the
index must be good enough to route correctly — an agent reads the index and decides
from one line per entry whether a page is worth loading. If an agent cannot tell from
`index.md` whether `module-payments.md` is relevant to the current review, the index
entry is too vague.

Write index entries to answer the question: *"Is this page relevant to what I am
currently reviewing?"* — not as a summary of contents.

---

## 1. Define What NOT to Write

This is more important than defining what to write. The biggest failure mode for an
autonomous wiki is bloat with low-value observations.

**Rule**: write to the wiki only when you discover something a future agent could not
re-derive by reading the current code or git history.

| Worth writing | Not worth writing |
|---|---|
| Architectural intent behind a non-obvious design | "The auth module handles tokens" (visible in code) |
| A recurring bug pattern in a module | "PR #42 fixed a null check" (in git history) |
| A known performance hotspot | Function signatures or file contents |
| A team norm ("X is treated as a hard requirement") | Findings specific to one PR |
| A constraint not visible in code ("never use library Y here because...") | Anything derivable from reading the current files |

Bake this into the Analysis agent's system prompt, not just the tool description.
A concrete prompt framing:

> *"Before calling memory_write, ask: would a future reviewer be wrong or
> inefficient without this? If they could figure it out in under 30 seconds from
> the code, skip it."*

---

## 2. Staleness Metadata on Every Page

Wiki pages written in PR #3 have no mechanism to be questioned in PR #300. Add a
standard header to every module and domain page:

```markdown
---
last_updated_pr: 47
last_updated_sha: a3f9c2d
---
```

When an Analysis agent reads a page and the SHA is old relative to the current PR's
base, it should treat the content as potentially stale and verify claims against the
actual code before trusting them. This is a prompt instruction, not just a data field.

The background maintenance workflow (see §6) uses these headers to prioritise which
pages to audit first.

---

## 3. Index Granularity

One entry per domain concept or module — not one entry per PR finding, and not one
mega-entry per layer. Too fine → index bloats past the always-loaded budget. Too
coarse → agents cannot distinguish relevant pages and load everything.

Target: each entry is one line, under 150 characters:

```
module-auth.md      | auth module — JWT lifecycle, token refresh, session validation
module-payments.md  | payments module — Stripe integration, webhook handling, idempotency
domain.md           | cross-cutting domain concepts and invariants
users/alice.md      | Alice Chen — owns auth and infra, strict on security review
```

---

## 4. `users/` Pages Are Underspecified but High-Value

These pages make reviews more accurate and appropriately toned. Useful content:

- **Domain ownership**: "Alice owns the auth module; defer to her on security
  decisions — do not block a PR on auth style if she has approved it."
- **Recurring patterns**: "Bob's PRs consistently lack error handling on external
  calls — check boundary code carefully."
- **Review preferences**: "Carol prefers suggestions framed as questions rather than
  directives."

These are hard to re-derive from code, do not go stale quickly, and directly change
how Argus should respond. Prompt the Analysis agent to write to `users/` when it
notices something persistent about a contributor — not per-PR observations, but
patterns seen across multiple PRs.

---

## 5. PR Thread Compaction — Concrete Threshold

The compaction pattern (summary + `last_comment_id` marker, load only newer comments)
is correct. The trigger needs a concrete number to be implementable:

**Suggested threshold**: compact when
`new_comments_since_last_compact > 30` OR `estimated_tokens(summary + new_comments) > 6000`

The compacted summary must include:
- Key decisions made in the thread
- Unresolved questions
- Argus's last review verdict and the commit SHA it reviewed
- `last_comment_id` (the anchor for future incremental loads)

Do not include: individual nitpick comments, resolved threads, duplicate observations.

---

## 6. Decay and Background Maintenance

Planned: a background LLM/workflow that runs periodically to audit the wiki. This is
the right design. Specific checks it should perform:

| Check | Signal | Action |
|---|---|---|
| **Stale pages** | `last_updated_sha` is N+ commits behind default branch HEAD | Re-verify claims against current code; update or flag |
| **Orphan pages** | Page exists in `index.md` but the module/path it describes no longer exists | Remove from index; archive or delete page |
| **Index gaps** | Module exists in `structure.md` but has no wiki page | Create a stub page so future agents know to populate it |
| **Contradictions** | Two pages make conflicting claims about the same module or concept | Resolve to one authoritative statement |
| **Bloated pages** | Page exceeds token budget (e.g. > 2K tokens) | Summarise or split |

The maintenance workflow does not need the full PR pipeline. It needs:
- `CodeReader.fetch_tree()` and `CodeReader.fetch_file()` to read current code
- `memory_read` / `memory_write` access to the wiki filesystem
- A ReAct agent with a "wiki auditor" system prompt

Trigger options (decide later, design supports all):
- Scheduled (e.g. weekly cron in `github_connect`)
- After every N merged PRs
- On-demand via a comment command (`@argus audit-wiki`)

---

## 7. Summary of Principles

1. **Index routes; pages inform.** Keep `index.md` as a decision aid, not a knowledge base.
2. **Write what cannot be re-derived.** If the code says it, skip the wiki entry.
3. **Every page carries its age.** `last_updated_pr` + `last_updated_sha` on every page.
4. **Concrete compaction threshold.** Pick a number now; tune it later.
5. **`users/` pages are first-class.** Contributor context changes review quality.
6. **Background maintenance closes the decay loop.** Staleness, orphans, and contradictions are handled by a dedicated workflow, not by hoping agents self-correct.
