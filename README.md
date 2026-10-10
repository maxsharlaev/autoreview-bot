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
2. **Webhook endpoint** (`POST /api/v1/pull-request`) requires only a valid `X-Hub-Signature-256` HMAC-SHA256 signature computed with `GITHUB_WEBHOOK_SECRET`; no API key is needed. If `GITHUB_WEBHOOK_SECRET` is not configured (empty, a placeholder like `change-me`, or shorter than 16 characters), the webhook endpoint is disabled (returns 503) and a startup warning is logged. **Manual/Actions endpoint** (`POST /api/v1/reviews`) requires `REVIEW_API_KEY` and remains available even when the webhook is disabled.
3. Checks the configured repository list and skips forks and draft PRs. An empty `github.allowed_repos` permits all repositories (a startup warning is logged); configure an explicit allowlist for production use.
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

`POST /api/v1/reviews` and `GET /api/v1/reviews/{id}` return `401` without a matching `REVIEW_API_KEY`. Send it as:

- `Authorization: Bearer <REVIEW_API_KEY>`
- `X-Api-Key: <REVIEW_API_KEY>`
- HTTP Basic for direct API calls (the password is `REVIEW_API_KEY`)

Store the same value in GitHub as Actions secret `AI_REVIEW_API_KEY` if using the reusable workflow. The webhook endpoint (`POST /api/v1/pull-request`) uses only `X-Hub-Signature-256` for authentication (GitHub webhooks cannot send custom API key headers). If `GITHUB_WEBHOOK_SECRET` is not configured, the webhook endpoint is disabled (503) but the service starts and `/api/v1/reviews` remains available. Do not place credentials in a webhook URL.

## Configuration

| Source | Purpose |
| --- | --- |
| `.env` | Secrets (`REVIEW_API_KEY`, GitHub App **or** `GITHUB_TOKEN` PAT, Jira, OpenAI), `WORKER_MAX_JOBS` |
| `config.yaml` | Allowlist (`github.allowed_repos` — a list, add one line per repo), Jira, Slack, schedule, language, Codex limits |

Copy `config.example.yaml` → `config.yaml`. Restart the API and worker after changing YAML (`get_app_config` is cached). `language.summary` sets the language of the model's summary and finding titles; `language.details` sets finding scenario/evidence/recommendation and PR comment labels. Both accept `en` or `ru`; English is the default. For Russian output, set both to `ru`. Jira/Slack text currently uses English labels; versioned templates and per-repository localization are planned. Slack `enabled: false` means the digest is stored/logged only.

To replace the review instructions without editing the repository's default, copy `prompts/review.md` to `prompts/local/review.md`, edit the copy, and set `codex.prompt_file: prompts/local/review.md` in `config.yaml`. The custom file replaces the entire instruction section; keep the JSON schema, trust-boundary and output-language rules you need. `{{summary_language}}` and `{{details_language}}` are substituted from `language` settings. `prompts/local/` is excluded from Git and Docker build context, then mounted read-only into the worker by Compose. A missing configured file fails the review rather than silently using the default. Completed runs record a hash of custom instructions as `prompt_version`. Restart the worker after changing the YAML; edits to the prompt file itself are read on the next review.

### Multiple owners

One deployment can serve several GitHub owners (organizations or accounts), each with its own GitHub credentials, webhook secret, API key, Jira, Slack, policies and model key. A config without `owners:` keeps working exactly as before.

| Mode | Detected by | Owners | Default owner |
| --- | --- | --- | --- |
| A. Legacy | no `owners:` block | `default`, built from today's env and top-level `github`/`jira`/`slack` | `default` |
| B. Legacy + extra owners | legacy GitHub credentials in env **and** an `owners:` block | `default` plus the listed owners | `default`, unless an owner has `default: true` |
| C. Single owner block | no legacy GitHub credentials, one owner | that owner | that owner |
| D. Several owners | no legacy GitHub credentials, two or more owners | the listed owners | the one with `default: true`, else the first one (startup warning) |

Two `default: true` owners, an owner named `default` in mode B, the same repo or `org/*` claimed by two owners, the same App installation or API key on two owners, or a missing required secret stop startup with an error that names the variable, never its value.

**Routing.** A request goes to: the explicit owner (`/api/v1/pull-request/<id>`, `X-Review-Owner` header, `?owner=` or an owner-scoped API key); else the owner listing the exact `org/repo` in `github.allowed_repos`; else the owner with `org/*`; else the owner whose non-zero `installation_id` matches the webhook; else `routing.unclaimed: default` (default owner) or `reject` (`unknown_owner`). The chosen owner's allowlist then applies. An explicit owner on a repo claimed by another owner gets 409 `owner_repo_conflict`; a webhook signed with one owner's secret for another owner's repo gets 403 `owner_signature_mismatch`. `REVIEW_API_KEY` is the operator key for all owners; `owners.<id>.api.key_env` is a key scoped to one owner, which sees only its own runs.

**Secrets.** YAML holds only env var names (`*_env`) or key file paths; a literal secret is rejected. Without an explicit `*_env` the name is `OWNER_<ID>_<NAME>` (`<ID>` upper-cased, `-` → `_`): `GITHUB_TOKEN`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, `REVIEW_API_KEY`, `JIRA_API_TOKEN`, `SLACK_BOT_TOKEN`, `OPENAI_API_KEY`; for example `OWNER_EXAMPLE_ORG_GITHUB_TOKEN`. `DATABASE_URL`, `REDIS_URL`, `OPENAI_API_KEY` (shared default model key) and worker/log/metrics settings stay shared. An invalid webhook secret disables webhooks only for that owner.

### Multiple owners: keeping pre-multi-owner history

Findings, review runs and comment-author records written before the `owners:` block existed are stored under the legacy owner id `default`. They are bound explicitly, never through the `default: true` flag:

- legacy env credentials only, or legacy env plus an `owners:` block: the legacy `default` owner keeps them;
- `owners:` block only: the owner that lists `default` in `aliases` owns them (only one owner may);
- otherwise nobody sees them: previous findings, previous head SHA, digest blockers and trusted comment authors from that history are ignored, and the API/worker log a startup warning.

When moving from legacy env credentials to named owners only, add the alias to the owner that should inherit that history:

```yaml
owners:
  example-org:
    default: true          # routing fallback only; moving it does not move history
    aliases: [default]     # inherits rows recorded as owner 'default'
    github:
      auth: pat
```

### Multiple owners: Jira and Slack per owner

A named owner uses only its own `jira` and `slack` blocks; nothing is inherited from the top-level sections or the legacy `default` owner. Without a `jira` block the owner makes no Jira requests; without a `slack` block it never posts, including the hourly digest. Jira reads, comments and transitions use the owner's site, account and `projects` (allowed project keys and their `rework_status`). Each owner with Slack gets its own digest in its own channel. Secrets are env var names: `api_token_env` / `bot_token_env`, or by default `OWNER_<ID>_JIRA_API_TOKEN` / `OWNER_<ID>_SLACK_BOT_TOKEN`. To share a Jira account or Slack bot between owners, point both at the same env var name.

```yaml
owners:
  example-org:
    github: { auth: pat, allowed_repos: [example-org/*] }
    jira:
      base_url: https://example-org.atlassian.net
      email: review-bot@example-org.example
      api_token_env: OWNER_EXAMPLE_ORG_JIRA_API_TOKEN
      projects:
        ABC: { rework_status: "In Progress" }
    slack:
      enabled: true
      channel: "#example-org-review"
      bot_token_env: OWNER_EXAMPLE_ORG_SLACK_BOT_TOKEN
```

A disabled owner's webhook URL `/api/v1/pull-request/<id>` answers `owner_disabled` only to requests signed with that owner's webhook secret; other requests get 401.

### Multiple owners: policies and model per owner

`public_repos`, `language`, `features` (`jira_comment`, `jira_transition`, `digest_enabled`), `pr_description` and `size_guard` can be overridden per owner. Overrides are partial and deep-merged over the top-level sections: unset fields keep the global value, and the merged section is validated like the global one (for example, `size_guard.hard` must stay at least `size_guard.soft`). They apply to everything that reads them for that owner's runs: the review prompt and comment, public-repository redaction, the size guard and its `override_label` (also in the webhook label check), PR description drafts, Jira comments/transitions and the digest. The digest schedule stays global; `features.digest_enabled` decides per owner.

`codex.model` and `codex.reasoning_effort` can be set per owner; other `codex` fields (sandbox, approval policy, limits, prompt file) stay global. The model key for an owner's runs is `codex.api_key_env` (required when set), else `OWNER_<ID>_OPENAI_API_KEY` if present, else the shared `OPENAI_API_KEY`. Only that key reaches the Codex sandbox, and only for that owner's runs; other owners' keys and owner secrets never enter the Codex or git environment.

```yaml
owners:
  example-org:
    github: { auth: pat, allowed_repos: [example-org/*] }
    public_repos: { security_findings: redact_all }
    language: { summary: ru, details: ru }
    features: { jira_transition: false, digest_enabled: false }
    size_guard:
      hard: { commits: 100 }
      override_label: example:force-review
    codex:
      model: gpt-5.6-sol
      reasoning_effort: high
      api_key_env: OWNER_EXAMPLE_ORG_MODEL_KEY
```

### Multiple owners: upgrade and rollback

1. Deploy the new image and run `alembic upgrade head` (Compose does this in `migrate`) with the config untouched: behaviour is unchanged and logs/metrics show `owner=default`.
2. Add `owners.<id>` and its `OWNER_<ID>_*` env, then restart API and worker; the API startup log shows the number of owners, the default owner and any owner config warnings.
3. Connect the new owner's events: install the same GitHub App in the other organization (same env names, `allowed_repos: [org-b/*]` or `installation_id`), or give the owner its own App/PAT with a webhook to `/api/v1/pull-request/<id>`.
4. Check a manual `POST /api/v1/reviews` with and without `X-Review-Owner`, a test PR in each organization, and `owner` in `GET /api/v1/reviews/{id}`.
5. Optional: move the legacy setup into a named owner with `default: true`, `aliases: [default]` and `*_env` pointing at the existing variable names (see above).
6. Rollback within multi-owner versions: remove or disable the `owners.<id>` block and restart; runs queued for that owner are skipped with `owner_not_configured` / `owner_disabled`, and its rows stay in the database.
7. Rollback to an image from before multi-owner support: with the new image, run `alembic downgrade 004_finding_details_visibility` first, then deploy the old image. The old image cannot use the upgraded schema as is: after `007_findings_owner_id`, `findings.owner_id` is required without a default, and after `006_comment_authors_objects` comment authors are stored as objects. If findings of several owners share a stable id on one pull request, the `007` downgrade refuses and reports the count; set `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1` to keep the repository owner's row (or the oldest) and delete the others together with their transitions.

## Optional PR description draft

Set `pr_description.enabled: true` in `config.yaml` to draft a description from the PR title, branch, commit messages, changed-file summary and an available Jira issue. The feature is off by default. `pr_description.mode` selects `comment` (a separate sticky suggestion), `fill_empty` (an empty body or unchanged default repository template), or `append` (a marked block below the author's text). Only an intact generated block is updated on later runs; edits by the author cause the update to be skipped. The model runs in the read-only sandbox and cannot write to GitHub directly. The review is published and saved before this optional step starts; `pr_description.timeout_seconds` sets its budget. Draft and fork PRs are skipped: they are not part of the bot's trusted review flow. Without Jira, the linked-task section is omitted. See [PR description configuration](docs/PR_DESCRIPTION.md).

Optional `pr_description.title_mode` supports `off` (default), `until_human_edit`, and `when_invalid_or_inconsistent`. The bot changes only a blank title, a branch-name title, or its own previously recorded title; it preserves any other human title. `always` remains a deprecated alias for `until_human_edit`. With `check_title_relevance: true`, unresolved mismatches appear in the draft. `pr_description.language` accepts `auto` or a language code; if omitted it follows the legacy `pr_text.language` key, then `language.details`. `size_guard` limits model use for large PRs, with `autoreview:force` as an override label.

## Public repository disclosure

The worker confirms repository visibility from GitHub on every run. Missing or conflicting visibility is treated as public. Webhook visibility is retained as a conservative hint. Existing configuration uses the public defaults below; private repositories keep the full review output.

```yaml
public_repos:
  jira_disclosure: key_only # none | key_only | full
  security_findings: redact # redact | redact_all | full
```

For public repositories, `key_only` shows the Jira key as plain text; `none` removes the linked key from bot output. `full` explicitly allows the linked task's Jira content in the generated description. The review and PR text models do not receive issue text fetched from Jira; a separate read-only alignment check receives it and publishes only `matches`, `partial`, `mismatch`, or `unknown`. When Jira data is available, this adds a model call, latency, and model cost to each public PR review. Previous findings remain available for status tracking. Details from findings created while private (`details_visibility`) are withheld from the public model context; details created while public are reused.

When `security_findings: redact` is set (the default), findings are redacted if they have a `security` category or if their title, scenario, evidence, or recommendation matches security-related keywords (injection, XSS, SSRF, CSRF, RCE, path traversal, deserialization, auth bypass, privilege escalation, secret, token leak, credential, and similar) or a CWE reference (`CWE-\d+`). This keyword heuristic does not guarantee detection of every mislabeled vulnerability; for strict setups, use `redact_all` to redact all finding categories unconditionally. Redacted findings show only severity, path/line, and a generic label. Full details remain in the database and go to configured Jira comments or Slack; configure Slack only with a private channel restricted to the intended reviewers. If neither destination is configured, the PR comment says details are hidden.

When a private visibility recheck fails or reports a public repository, publication uses a generic public-safe version; the same reduced text is sent to Jira and Slack for that run. A changed PR head fails the run with `HEAD_CHANGED`. `security_findings: full` explicitly restores detailed GitHub findings, except older private details. Existing comments are not rewritten.

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
- `POST /api/v1/pull-request` — GitHub webhook (`X-Hub-Signature-256` required, no API key)
- `GET /health` — liveness
- `GET /is-ready` — Postgres + Redis
- `GET /metrics` — Prometheus (API). Worker: `http://localhost:9100/metrics`

**Observability:** scrape `GET /metrics` (API `:8000`, worker `:9100`) with **your** Prometheus. Import [observability/grafana/dashboards/autoreview-bot.json](observability/grafana/dashboards/autoreview-bot.json) into existing Grafana. Setup: [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md). On-call: [docs/OPERATIONS.md](docs/OPERATIONS.md).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the pipeline and data model, and [docs/ROADMAP.md](docs/ROADMAP.md) for planned work. Licensed under [Apache-2.0](LICENSE). Copyright 2026 Maksim Sharlaev.
