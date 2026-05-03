# Argus Deployment Guide (Local + Production)

This guide covers how to run Argus end-to-end with:

- `github_connect` (FastAPI webhook ingress + GitHub adapters)
- `agent_core` (review orchestration/pipelines)
- `llm_framework` (LLM client abstraction)

It is written for the current codebase shape and env variables.

## 1) Prerequisites

- Python 3.12+ (project currently runs on 3.13 in local tests)
- A GitHub App configured for webhook delivery
- A reachable HTTPS URL for GitHub webhooks (local tunnel for dev)
- Valid LLM API credentials for all configured tiers

## 2) Parameter Reference

This section documents each runtime parameter, what it is used for, and where it is consumed.

### GitHub connect layer

| Parameter | Required | Used for |
|---|---|---|
| `GITHUB_APP_ID` | Yes | GitHub App issuer ID used to mint JWT (`github_connect/auth.py`) |
| `GITHUB_WEBHOOK_SECRET` | Yes | Verifies `X-Hub-Signature-256` webhook HMAC (`github_connect/server.py`) |
| `GITHUB_APP_PRIVATE_KEY_PEM` | Yes* | PEM private key content used to sign app JWT (`github_connect/auth.py`) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Yes* | File path to PEM key if not using inline env value (`github_connect/auth.py`) |
| `ARGUS_STATE_DB_PATH` | Yes | SQLite file path for webhook idempotency/status state (`github_connect/state_store.py`) |

\* Provide one of `GITHUB_APP_PRIVATE_KEY_PEM` or `GITHUB_APP_PRIVATE_KEY_PATH`. If both are set, `..._PEM` wins.

### Agent core + model routing

| Parameter | Required | Used for |
|---|---|---|
| `ARGUS_CONFIG_PATH` | No | Path to TOML config; default `/etc/argus/config.toml` (`agent_core/config.py`) |
| `ARGUS_WIKI_ROOT` | Yes (unless in TOML) | Root folder for memory/wiki files (`agent_core/config.py`) |
| `ARGUS_BOT_USERNAME` | Yes (unless in TOML) | Bot identity for self-comment filtering and behavior (`agent_core/config.py`) |
| `ARGUS_REVIEW_DRAFTS` | No | Enable/disable review on draft PRs (bool) |
| `ARGUS_ACKNOWLEDGE_EVENTS` | No | Enable/disable lightweight acknowledgement comments (bool) |
| `ARGUS_STRUCTURE_SYNC_ENABLED` | No | Enable hybrid `structure.md` bootstrap/self-heal/auto-refresh (bool; default false) |
| `ARGUS_VERDICT_THRESHOLD` | No | Minimum severity for verdict decisions (`critical/high/medium`) |

### Per-tier LLM credentials

| Parameter | Required | Used for |
|---|---|---|
| `ARGUS_FAST_API_KEY` | Yes (unless in TOML) | API key for fast tier |
| `ARGUS_STANDARD_API_KEY` | Yes (unless in TOML) | API key for standard tier |
| `ARGUS_DEEP_API_KEY` | Yes (unless in TOML) | API key for deep tier |
| `ARGUS_FAST_PROVIDER` / `ARGUS_STANDARD_PROVIDER` / `ARGUS_DEEP_PROVIDER` | No | Provider override per tier (`anthropic`, `ollama`, `openrouter`) |
| `ARGUS_FAST_MODEL` / `ARGUS_STANDARD_MODEL` / `ARGUS_DEEP_MODEL` | No | Model override per tier |

### OpenRouter-specific (only when provider=`openrouter`)

| Parameter | Required | Used for |
|---|---|---|
| `OPENROUTER_BASE_URL` | No | OpenRouter API base URL (default `https://openrouter.ai/api/v1`) |
| `OPENROUTER_APP_NAME` | No | Sent as `X-Title` request header |
| `OPENROUTER_APP_URL` | No | Sent as `HTTP-Referer` request header |

### Ollama-specific (only when provider=`ollama`)

| Parameter | Required | Used for |
|---|---|---|
| `ARGUS_OLLAMA_HOST` | No | Ollama host URL (default `http://localhost:11434`) |

### Limits / tuning (optional)

All are optional and can be set in TOML (`[limits]`) or env:
- `ARGUS_MAX_PLANS`
- `ARGUS_MAX_INLINE_SUGGESTIONS`
- `ARGUS_MAX_SEARCH_RESULTS`
- `ARGUS_MAX_FILE_WINDOW_LINES`
- `ARGUS_THREAD_COMPACT_THRESHOLD`
- `ARGUS_PRELOAD_DIFF_THRESHOLD`

### Recommended TOML pattern (per-tier models, env secrets)

Use TOML for provider/model routing and keep API keys in environment variables.

```toml
[argus]
wiki_root = "/var/lib/argus/wiki"
bot_username = "argus-bot"

[clients.fast]
provider = "openrouter"
model = "anthropic/claude-3.5-haiku"

[clients.standard]
provider = "openrouter"
model = "anthropic/claude-sonnet-4"

[clients.deep]
provider = "openrouter"
model = "anthropic/claude-opus-4"
```

Then set:

- `ARGUS_FAST_API_KEY`
- `ARGUS_STANDARD_API_KEY`
- `ARGUS_DEEP_API_KEY`

## 3) App Entry Point

A concrete root entrypoint already exists at `app.py`. It:

- loads `ArgusConfig` from env/TOML
- builds `ClientTier` for `fast/standard/deep`
- supports `anthropic` and `ollama` providers from `clients.<tier>.provider`
- supports `anthropic`, `ollama`, and `openrouter` providers from `clients.<tier>.provider`
- wraps clients with `RetryClient`
- constructs `Dispatcher` and exposes `app = create_app(...)`

If using `ollama`, optionally set:

- `ARGUS_OLLAMA_HOST` (default: `http://localhost:11434`)

If using `openrouter`:

- set each tier API key to your OpenRouter key (`ARGUS_FAST_API_KEY`, `ARGUS_STANDARD_API_KEY`, `ARGUS_DEEP_API_KEY`)
- set each tier provider to `openrouter`
- set each tier model to an OpenRouter model ID (for example `anthropic/claude-sonnet-4`)
- optionally set `OPENROUTER_BASE_URL`, `OPENROUTER_APP_NAME`, `OPENROUTER_APP_URL`

Run with:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

## 4) Local Development Deployment

1. Export env vars.
2. Start service locally:

```bash
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

If provider SDKs are missing, install what you use:

```bash
python -m pip install fastapi uvicorn anthropic ollama openai
```

3. Expose local port with a tunnel (for example `ngrok`, `cloudflared`, etc.).
4. Configure GitHub App webhook URL:
   - `https://<tunnel-domain>/webhooks/github`
5. In GitHub App settings:
   - set **Webhook secret** to match `GITHUB_WEBHOOK_SECRET`
   - subscribe to:
     - `Pull requests`
     - `Issue comments`
     - `Pull request review comments`
   - install the app to your target repo/org
6. Copy values from GitHub App settings into runtime config:
   - **App ID** -> `GITHUB_APP_ID`
   - **Private key (.pem)** -> `GITHUB_APP_PRIVATE_KEY_PATH` (or paste into `GITHUB_APP_PRIVATE_KEY_PEM`)
   - **Webhook secret** -> `GITHUB_WEBHOOK_SECRET`
7. Trigger events:
   - open PR
   - push commit to PR
   - comment on PR

Health check:

```bash
curl http://127.0.0.1:8000/healthz
```

Expected: `{"ok": true}`.

## 5) Production Deployment

## Runtime model

- Single-process v1 is the intended model.
- SQLite state DB is used for delivery idempotency/status tracking.
- Per-PR concurrency ordering is enforced in `agent_core`.

## Recommended process setup

- Run behind a reverse proxy/load balancer with HTTPS termination.
- Keep one app instance per state DB file.
- Use persistent disk for:
  - `ARGUS_STATE_DB_PATH` (SQLite)
  - `ARGUS_WIKI_ROOT` (wiki memory files)
- Configure structured log collection.

## Suggested uvicorn command

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
```

Use `--workers 1` for v1 to stay consistent with single-process assumptions.

## Secret management

- Provide private key via mounted file (`GITHUB_APP_PRIVATE_KEY_PATH`) or secret env (`..._PEM`).
- Do not commit secrets to repo.
- Rotate webhook secret and private key via your secret manager process.

## 6) Operational Runbook

## Key endpoints

- Webhook ingress: `POST /webhooks/github`
- Health: `GET /healthz`
- Optional debug endpoint: `POST /debug/dispatch` (only if enabled)

## Expected webhook behaviors

- Invalid signature: `401`
- Valid + accepted for processing: `202`-style accepted payload
- Duplicate processed delivery: skipped
- Unsupported event/action: marked ignored

## Troubleshooting checklist

1. `401 invalid signature`
   - verify GitHub webhook secret exactly matches `GITHUB_WEBHOOK_SECRET`
2. No processing after delivery accepted
   - inspect app logs for dispatch exceptions
   - check SQLite `webhook_deliveries` status/error
3. GitHub API auth failures
   - verify `GITHUB_APP_ID`
   - verify private key validity and format
   - verify app installation exists for target repo
4. LLM failures
   - verify all `ARGUS_*_API_KEY` env vars
   - inspect retry/failure logs
5. Missing memory/wiki behavior
   - verify `ARGUS_WIKI_ROOT` is writable and persistent

## 7) Security Notes

- Always run webhook endpoint over HTTPS in production.
- Verify signature before any processing (already implemented).
- Keep debug dispatch endpoint disabled in production unless explicitly needed.
- Restrict outbound network access to required providers where possible (GitHub + LLM providers).
