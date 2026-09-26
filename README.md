# autoreview-bot

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![CI](https://github.com/maxsharlaev/autoreview-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/maxsharlaev/autoreview-bot/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Self-hosted FastAPI service for advisory pull request reviews. The current implementation reads GitHub PRs, runs Codex CLI, records findings, and updates a PR comment. It can read a linked Jira issue; Jira comments and status transitions are configurable. Slack notifications are disabled by default.

**Status:** early MVP. Reviews do not block merges. The code has unit tests and CI, but the full GitHub → worker → Jira path has not yet been verified by automated integration tests. See the [roadmap](docs/ROADMAP.md) for planned integrations and the [public release checklist](docs/PUBLIC_RELEASE_CHECKLIST.md) for publication gates.

**Supported now:** GitHub, Codex CLI, Jira and optional Slack. Other code hosts, issue trackers, reviewer CLIs, independent model validation, an optional admin UI, and localized output templates are planned.

**Deployment:** [docs/DEPLOY.md](docs/DEPLOY.md). **GitHub App / PAT / webhook:** [docs/GITHUB_SETUP.md](docs/GITHUB_SETUP.md). **Russian documentation:** [docs/ru/](docs/ru/README.md).

## What it does

1. GitHub App webhook `POST /api/v1/pull-request` on `opened`, `synchronize`, `reopened`, `ready_for_review`.
2. Requires access key `REVIEW_API_KEY` (GitHub secret `AI_REVIEW_API_KEY`). If a request includes `X-Hub-Signature-256`, the service also verifies it against `GITHUB_WEBHOOK_SECRET`.
3. Checks the configured repository list and skips forks and draft PRs. An empty `github.allowed_repos` currently permits all repositories; configure a non-empty list for use outside local testing.
4. Reuses a pending, running, or completed review for the same `{repo}:{pr}:{head_sha}` under normal sequential delivery. A new SHA requests cancellation of the previous job. Manual `POST /api/v1/reviews` defaults to `force: true`. Different PRs can run in parallel (`WORKER_MAX_JOBS`, default 4). Atomic deduplication and a final SHA check are planned.
5. Worker loads PR diff, extracts a Jira key, builds untrusted-marked context, clones the head SHA, runs `codex exec` in a read-only sandbox.
6. Validates the JSON schema, stores findings, and updates one sticky PR comment (`<!-- open-pr-review -->`). Stricter SHA and location checks are planned.
7. If the issue exists and flags are on: Jira comment (caught / checked / result) and transition to `rework_status` when P0/P1 findings exist.
8. Hourly (configurable) digest of PRs in the local store, refreshed from GitHub; posted to Slack only if enabled. Closure reconciliation and pagination are planned.

## Quick start

```bash
cp .env.example .env
cp config.example.yaml config.yaml
# fill POSTGRES_PASSWORD (URL-safe random value), REVIEW_API_KEY, GitHub App or GITHUB_TOKEN, and OpenAI credentials
# configure github.allowed_repos; Jira credentials are needed only when Jira is enabled
docker compose up --build
```

API listens on `http://localhost:8000`. Use the [reusable GitHub Actions workflow](examples/github/ai-review-trigger.yml) or the manual endpoint for a first test. Direct GitHub webhook deployment needs the signed webhook auth change in the roadmap.

Local Python:

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -e ".[dev]"
# set DATABASE_URL / REDIS_URL and run migrations
alembic upgrade head
uvicorn app.main:app --reload
arq app.workers.settings.WorkerSettings
```

## Access key

`POST /api/v1/pull-request`, `POST /api/v1/reviews`, and `GET /api/v1/reviews/{id}` return `401` without a matching key. Send it as:

- `Authorization: Bearer <REVIEW_API_KEY>`
- `X-Api-Key: <REVIEW_API_KEY>`
- HTTP Basic for direct API calls (the password is `REVIEW_API_KEY`)

Store the same value in GitHub as Actions secret `AI_REVIEW_API_KEY` if using the reusable workflow. The webhook currently accepts a valid API key without an HMAC header. Requiring a signature for webhook delivery is part of the public release roadmap. Do not place credentials in a webhook URL.

## Configuration

| Source | Purpose |
| --- | --- |
| `.env` | Secrets (`REVIEW_API_KEY`, GitHub App **or** `GITHUB_TOKEN` PAT, Jira, OpenAI), `WORKER_MAX_JOBS` |
| `config.yaml` | Allowlist (`github.allowed_repos` — a list, add one line per repo), Jira, Slack, schedule, language, Codex limits |

Copy `config.example.yaml` → `config.yaml`. Restart the API and worker after changing YAML (`get_app_config` is cached). `language.summary` sets the language of the model's summary and finding titles; `language.details` sets finding scenario/evidence/recommendation and PR comment labels. Both accept `en` or `ru`; English is the default. For Russian output, set both to `ru`. Jira/Slack text currently uses English labels; versioned templates and per-repository localization are planned. Slack `enabled: false` means the digest is stored/logged only.

To replace the review instructions without editing the repository's default, copy `prompts/review.md` to `prompts/local/review.md`, edit the copy, and set `codex.prompt_file: prompts/local/review.md` in `config.yaml`. The custom file replaces the entire instruction section; keep the JSON schema, trust-boundary and output-language rules you need. `{{summary_language}}` and `{{details_language}}` are substituted from `language` settings. `prompts/local/` is excluded from Git and Docker build context, then mounted read-only into the worker by Compose. A missing configured file fails the review rather than silently using the default. Completed runs record a hash of custom instructions as `prompt_version`. Restart the worker after changing the YAML; edits to the prompt file itself are read on the next review.

## Manual local review (no GitHub webhook)

The GitHub webhook is optional for a first test. Call the local API yourself. The worker still uses `GITHUB_TOKEN` (or GitHub App) to **read the PR, clone the SHA, and post the sticky comment** — you only skip GitHub delivering the event.

1. Service up: `docker compose up --build`, then `GET http://localhost:8000/health` and `GET http://localhost:8000/is-ready`.
2. Repo on the allowlist in `config.yaml`:

```yaml
github:
  allowed_repos:
    - example-org/example-repo
    # - example-org/another-repo
```

The PAT must have access to every listed repository.

3. Start a run (pick an **open, non-draft** PR). PowerShell — paste the UUID from `.env` (`REVIEW_API_KEY`):

```powershell
$key = "your-uuid-here"
$headers = @{ Authorization = "Bearer $key" }

# Option A — PR URL
$body = @{ pull_url = "https://github.com/example-org/example-repo/pull/1" } | ConvertTo-Json
Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/reviews -Headers $headers -ContentType "application/json" -Body $body

# Option B — owner/name + number
$body = @{ repository = "example-org/example-repo"; number = 1 } | ConvertTo-Json
Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/reviews -Headers $headers -ContentType "application/json" -Body $body
```

`curl` equivalent:

```bash
curl -sS -X POST http://localhost:8000/api/v1/reviews \
  -H "Authorization: Bearer $REVIEW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"repository":"example-org/example-repo","number":1}'
```

Postman:

1. Method **POST**, URL `http://localhost:8000/api/v1/reviews`
2. **Authorization** → Type **Bearer Token** → Token = `REVIEW_API_KEY` from `.env` (your UUID)
3. **Headers**: `Content-Type` = `application/json`
4. **Body** → **raw** → **JSON**, one of:

```json
{"pull_url": "https://github.com/example-org/example-repo/pull/1"}
```

Same SHA after a successful review: the webhook path reuses `completed`. The manual endpoint starts a new run unless you send `"force": false`.

```json
{"repository": "example-org/example-repo", "number": 1}
```

5. Send. Expect **202** and `review_run_id`. Then **GET** `http://localhost:8000/api/v1/reviews/<review_run_id>` with the same Bearer token.

Watch progress in the worker container (`docker compose logs -f worker`). The worker logs review progress plus Codex stdout/stderr and a heartbeat every 20 s.

Optional fields if you already know the SHAs (then the enqueue path does not call GitHub; the worker still does): `head_sha`, `base_sha`, `head_ref`, `title`, `html_url`.

Response `202`:

```json
{"status":"pending","review_run_id":"<uuid>","head_sha":"...","trigger":"manual"}
```

6. Poll status:

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/reviews/<review_run_id>" -Headers @{ Authorization = "Bearer $key" }
```

Watch `worker` logs for `review_run`. On success the PR gets a sticky comment (`<!-- open-pr-review -->`).

| HTTP | Meaning |
| --- | --- |
| 401 | `REVIEW_API_KEY` missing or wrong |
| 403 `repo_not_allowed` | repo not in `allowed_repos` |
| 422 | need `pull_url` **or** `repository` + `number` |
| 502 | GitHub API failed (PAT / permissions / PR missing) |

`REVIEW_API_KEY` is a password **you generate** (UUID is fine). It is not a GitHub PAT. Same value in `.env`; send it as `Authorization: Bearer ...`.

## Parallel PR reviews

The API enqueues each PR independently. The ARQ worker runs up to `WORKER_MAX_JOBS` Codex jobs at once (default `4`). Concurrent delivery and stale results need the additional safeguards listed in the roadmap before scaling to multiple workers.

## Jira

Service account needs browse + comment + transition on the listed projects. The service does not create issues.

## Trust boundaries

- Webhook payload, diff, PR text, and Jira text are untrusted and never interpolated into shell commands.
- `codex exec` runs with a stripped environment (`OPENAI_API_KEY` copied to `CODEX_API_KEY`; the default Codex provider ignores `OPENAI_API_KEY` alone), `--sandbox read-only`, and `--ask-for-approval never` before `exec`. Reads inside the checkout auto-run; writes, network, and escaping the sandbox are rejected — nobody is there to click prompts. The worker Compose service relaxes Docker seccomp/AppArmor so Codex's inner bubblewrap can create user namespaces; that is not `--yolo`.
- GitHub write, Jira, Slack, and database credentials stay in the orchestrator process, not in the Codex subprocess.
- Git clone uses `GIT_ASKPASS` (username `x-access-token`, password = PAT). The token is not kept on the remote URL after checkout.

## Endpoints

- `POST /api/v1/reviews` — manual trigger (access key, no HMAC). Body: `pull_url` or `repository` + `number`
- `GET /api/v1/reviews/{review_run_id}` — run status (access key required)
- `GET /api/v1/ops/status` — worker/queue snapshot (access key). Use this when reviews “do nothing”
- `POST /api/v1/pull-request` — GitHub webhook (access key required; HMAC if `X-Hub-Signature-256` is present)
- `GET /health` — liveness
- `GET /is-ready` — Postgres + Redis
- `GET /metrics` — Prometheus (API). Worker: `http://localhost:9100/metrics`

**Observability:** scrape `GET /metrics` (API `:8000`, worker `:9100`) with **your** Prometheus. Import [observability/grafana/dashboards/autoreview-bot.json](observability/grafana/dashboards/autoreview-bot.json) into existing Grafana. Setup: [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md). On-call: [docs/OPERATIONS.md](docs/OPERATIONS.md).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the pipeline and data model, and [docs/ROADMAP.md](docs/ROADMAP.md) for planned work. Licensed under [Apache-2.0](LICENSE). Copyright 2026 Maksim Sharlaev.
