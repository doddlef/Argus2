# Argus2

Argus2 is an automated GitHub PR review agent.

It has three layers:

- `github_connect`: webhook ingress, GitHub auth/adapters, event translation
- `agent_core`: review workflows, nodes, routing, memory/structure sync
- `llm_framework`: model/tool/workflow abstraction

## What It Does

- Handles GitHub webhook events for:
- `pull_request` (`opened`, `reopened`, `synchronize`)
- `issue_comment` (`created` on PRs)
- `pull_request_review_comment` (`created`)
- Runs PR analysis and posts:
- PR reviews (approve/comment/request_changes)
- PR comments (conversation mode, acknowledgements)
- Maintains project memory under `wiki/argus/<owner>/<repo>/`:
- `index.md`
- `structure.md`
- thread compaction and staged wiki ops

## Project Layout

- `app.py`: app entrypoint (`FastAPI` app + wiring)
- `agent_core/`: domain workflows and nodes
- `github_connect/`: GitHub integration layer
- `llm_framework/`: shared orchestration/client primitives
- `docs/`: design and deployment docs
- `tests/`: unit/integration tests

## Quick Start (Local Dev)

1. Install dependencies (minimum):

```bash
python -m pip install fastapi uvicorn httpx pydantic tomli
```

Add provider SDKs based on your model provider:

- Anthropic: `python -m pip install anthropic`
- OpenRouter: `python -m pip install openai`
- Ollama: `python -m pip install ollama`

2. Create config:

```bash
cp config.argus.toml.example config.argus.toml
```

3. Set required env vars:

```bash
export ARGUS_CONFIG_PATH="config.argus.toml"
export GITHUB_APP_ID="<app_id>"
export GITHUB_WEBHOOK_SECRET="<webhook_secret>"
export GITHUB_APP_PRIVATE_KEY_PATH="/abs/path/to/private-key.pem"
export ARGUS_STATE_DB_PATH="db/argus_state.db"
```

4. Run server:

```bash
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

5. Expose webhook endpoint (example with `smee`):

```bash
smee --url https://smee.io/yqPQdEmVdDOimOn --target http://127.0.0.1:8000/webhooks/github
```

6. Configure GitHub App webhook subscriptions:

- `Pull requests`
- `Issue comments`
- `Pull request review comments`

7. Health check:

```bash
curl http://127.0.0.1:8000/healthz
```

## Configuration

Primary config is TOML + env overrides.

- TOML path: `ARGUS_CONFIG_PATH` (example: `config.argus.toml`)
- Memory root: `ARGUS_WIKI_ROOT` (or `[argus].wiki_root` in TOML)
- Structure sync: `ARGUS_STRUCTURE_SYNC_ENABLED=true|false`
- State DB (idempotency): `ARGUS_STATE_DB_PATH`

LLM tiers are configured independently:

- `[clients.fast]`
- `[clients.standard]`
- `[clients.deep]`

Each tier supports provider/model and optional key (provider-dependent).

## Webhook Processing Model

- Validates `X-Hub-Signature-256` HMAC
- Applies idempotency guard on `X-GitHub-Delivery` via SQLite state store
- Translates payload into domain trigger
- Dispatches trigger through `agent_core` workflow
- Uses per-PR lock to keep ordering and avoid concurrent race on same PR

## Notes on Sync Runs

For `pull_request.synchronize`, Argus now carries base SHA (`before`) into session context and fetches delta files via GitHub compare API. Triage receives explicit delta context so follow-up runs can focus on what changed since last push.

## Testing

Run full tests:

```bash
pytest -q
```

Run key suites:

```bash
pytest -q tests/test_github_connect.py tests/test_pipeline.py
```

## Troubleshooting

- Webhook `401`: secret mismatch (`GITHUB_WEBHOOK_SECRET`)
- Event accepted but no review: check app logs for `Route`/`Analysis`/`Summary` nodes
- Repeated duplicate deliveries: verify `ARGUS_STATE_DB_PATH` is persistent/writable
- Missing conversation reply: verify webhook event subscriptions include both comment events

## Docs

- Core design: `docs/core_design.md`
- GitHub connect design: `docs/github_connect_design.md`
- Deployment guide: `docs/deployment.md`
