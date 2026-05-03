# github_connect — Design and Decision Record

Last updated: 2026-05-03

## Purpose

`github_connect` is the GitHub-coupled controller layer above `agent_core`.
It receives webhooks, verifies/authenticates requests, translates payloads into
`agent_core` triggers, implements `CodeReader` and `Commenter` ports, and
handles connector runtime concerns (idempotency, retries, observability).

This document captures the final decisions agreed during design discussion so
implementation can proceed without reopening major tradeoffs.

---

## 1) Scope and Boundaries

- `github_connect` owns:
  - FastAPI ingress and health/debug endpoints
  - HMAC verification (`X-Hub-Signature-256`)
  - GitHub App auth (JWT + installation tokens)
  - Translation from webhook payloads to `agent_core` triggers
  - Port adapters (`GitHubCodeReader`, `GitHubCommenter`)
  - Delivery idempotency/state tracking in SQLite
  - API transport retries/rate-limit handling/logging
- `github_connect` does **not** own:
  - Review routing/business logic (owned by `agent_core`)
  - Wiki storage logic (owned by `agent_core` tools)
  - Source-of-truth code storage (GitHub API remains source)

Deployment target for v1: **single process**.

---

## 2) Event Coverage and Routing

Supported webhook events/actions in v1:

1. `pull_request`: `opened`, `reopened`, `synchronize`
2. `issue_comment`: `created` (only when `issue.pull_request` exists)
3. `pull_request_review_comment`: `created`

Trigger mapping:

- `pull_request.opened|reopened` -> `PROpenedTrigger`
- `pull_request.synchronize` -> `PRSyncTrigger`
- supported comment events -> `CommentReceivedTrigger`

Ignore policy:

- Unsupported events/actions: return `200`, mark delivery as `ignored`
- Bot-authored comment events (issue/review comments): `ignored`
- Draft gating stays in `agent_core.Dispatcher` only (do not duplicate here)

---

## 3) Webhook Ingress and Idempotency

Request flow:

1. Verify HMAC signature
2. Parse minimal event metadata
3. Idempotency check/update in SQLite
4. Enqueue background processing
5. Return quickly

Responses:

- Bad/missing signature: `401`, do **not** persist delivery
- Enqueue success: `202`
- Enqueue failure: `500` (allow GitHub retry)

Idempotency model (`X-GitHub-Delivery`):

- Use persistent SQLite from day 1
- Skip only when status is `processed`
- Retry allowed for `failed`
- Retry allowed for stale `received` (stale threshold: **15 minutes**)

Delivery status values:

- `received`, `processed`, `failed`, `ignored`

---

## 4) State DB (SQLite)

Purpose:

- Webhook idempotency
- Delivery lifecycle visibility and debugging

Not used for:

- Wiki content
- Source code/PR truth data

Config:

- `ARGUS_STATE_DB_PATH` required

Schema strategy:

- Startup DDL only (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`)
- No migration framework in v1

Suggested table:

- `webhook_deliveries`
  - `delivery_id TEXT PRIMARY KEY`
  - `event TEXT NOT NULL`
  - `installation_id INTEGER`
  - `repo TEXT`
  - `status TEXT NOT NULL` (`received|processed|failed|ignored`)
  - `received_at TEXT NOT NULL`
  - `processed_at TEXT`
  - `error TEXT`

Suggested index:

- Index on `received_at`

---

## 5) GitHub Auth and Transport

Auth:

- Required env:
  - `GITHUB_APP_ID`
  - `GITHUB_APP_PRIVATE_KEY_PEM` or `GITHUB_APP_PRIVATE_KEY_PATH`
  - `GITHUB_WEBHOOK_SECRET`
  - `ARGUS_STATE_DB_PATH`
- Private key precedence:
  - `GITHUB_APP_PRIVATE_KEY_PEM` > `GITHUB_APP_PRIVATE_KEY_PATH`

Installation tokens:

- In-memory cache per `installation_id`
- Proactive refresh when < 5 minutes to expiry

HTTP client:

- Shared `httpx.AsyncClient`
- Timeout: 10s connect, 30s read
- Retry on `429` and `5xx` with exponential backoff
- Honor `Retry-After` when present
- Bounded retries; on exhaustion, surface error up to `agent_core`

API style:

- REST-only in v1 (no GraphQL)

---

## 6) Port Adapter Behavior

### `GitHubCommenter` (implements `Commenter`)

Verdict mapping:

- `approve` -> `APPROVE`
- `request_changes` -> `REQUEST_CHANGES`
- `comment` -> `COMMENT`

Inline review comments:

- Use modern fields:
  - single-line: `line=<end_line>`, `side=RIGHT`
  - range: `start_line=<start_line>`, `start_side=RIGHT`, `line=<end_line>`, `side=RIGHT`
- Do not use deprecated `position`

Failure degradation:

- If inline comments are rejected, degrade to body-only review
- Preserve original verdict when GitHub accepts verdict without inline comments
- Add note to review body about dropped inline comments

### `GitHubCodeReader` (implements `CodeReader`)

Caching:

- Required in-run file cache keyed by `(repo, ref, path)`
- Required in-run tree cache keyed by `(repo, ref)`
- Optional cross-delivery tree cache (default on, TTL 10 minutes)

Comments fetch:

- Merge issue comments + review comments chronologically
- `after` handling:
  - try to locate `after` comment ID in fetched set
  - return strictly newer comments when found
  - if not found, return all fetched comments (safe fallback)

Pagination and caps:

- Enforce hard cap for large PR surfaces (agreed baseline: up to 300 files / 20 pages)
- Log truncation clearly
- Surface truncation context to downstream prompts without port changes:
  - inject synthetic one-line commit message with prefix `[ARGUS_CONTEXT]`
  - optionally include description note when available

Per-run synthetic notes:

- Track adapter warnings in per-instance run state
- Keep synthetic context machine-identifiable and concise

---

## 7) Runtime and Concurrency

- Webhook handler is non-blocking: verify + dedupe + enqueue + return `202`
- Process deliveries concurrently across different PRs
- Same PR ordering enforced by `agent_core` PR lock
- No distributed coordination in v1 (single-process target)

---

## 8) Observability and Operational Endpoints

Structured log fields (required where known):

- `delivery_id`
- `installation_id`
- `repo`
- `event`
- `pr_number`

Endpoints:

- `POST /webhooks/github` — primary ingress
- `GET /healthz` — liveness + DB ping
- Optional `POST /debug/dispatch` — behind explicit env flag, disabled by default

---

## 9) Module Layout

Planned `github_connect` package split:

1. `server.py` — FastAPI routes and background dispatch wiring
2. `auth.py` — JWT signing + installation token management
3. `github_api.py` — raw GitHub REST transport wrappers
4. `adapters.py` — `GitHubCodeReader` and `GitHubCommenter`
5. `translator.py` — webhook payload -> trigger translation
6. `state_store.py` — SQLite idempotency/status store

---

## 10) Test Plan (Implementation Guidance)

Priority order:

1. Translator unit tests (event/action mapping + ignore rules)
2. State store tests (dedupe + stale retry + status transitions)
3. Adapter tests with mocked GitHub responses (pagination, caps, mapping, degradation)
4. One integration-style FastAPI webhook test (verify -> dedupe -> enqueue path)

---

## 11) Open Items (Deferred, Not Blockers)

- Multi-replica/distributed deployment semantics
- Stronger persistent caches beyond runtime metadata
- Advanced migration tooling for connector DB
- GraphQL-based optimizations

