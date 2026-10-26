<!-- section: architecture -->
# Architecture

Autoreview-bot is an advisory service: GitHub PR event → queue → Codex CLI → verified JSON → one updated PR comment. It does not block merges or create Jira issues. See the [roadmap](ROADMAP.md) and the [Russian translation](ru/ARCHITECTURE.md).

<!-- section: pipeline -->
## Pipeline

```text
GitHub PR event or Actions job
  -> POST /api/v1/pull-request (HMAC signature, no API key)
     or POST /api/v1/reviews (API key)
  -> Redis / ARQ (up to WORKER_MAX_JOBS jobs in parallel)
  -> Review worker
       -> GitHub API (PR, diff, author permission)
       -> Jira read (key from branch, title or body)
       -> Context builder (limits, untrusted blocks)
       -> git clone at head SHA (PAT or installation token via GIT_ASKPASS)
       -> codex exec --sandbox read-only --output-schema
       -> JSON verifier
       -> Postgres (runs / findings / transitions)
       -> Publisher: sticky comment, progress status
       -> Jira comment and status transition (P0/P1)
       -> Slack (if enabled)
Scheduler
  -> digest of open PRs
  -> Slack, or only a database record
```

<!-- section: components -->
## Components

| Component | Responsibility |
| --- | --- |
| `app/api/v1/pull_request.py` | Webhook ingress, signature check, owner routing |
| `app/api/v1/reviews.py` | Manual review API and run status |
| `app/services/review_enqueue.py` | Run creation, deduplication, cancellation of older SHAs |
| `app/adapters/github.py` | GitHub App JWT, installation token or PAT; PRs, diffs, comments, statuses |
| `app/adapters/jira.py` | Issue read, comment and status transition |
| `app/adapters/slack.py` | Optional notifications; disabled by default |
| `app/owners/` | Owner registry, routing, per-owner credentials and config |
| `app/services/orchestrator.py` | One review run |
| `app/services/codex_runner.py` | Codex subprocess with a restricted environment |
| `app/services/verifier.py` | JSON Schema, finding paths, count cap |
| `app/services/publisher.py` | Sticky comment, Jira write, Slack |
| `app/services/progress_status.py` | Check run / commit status ([PROGRESS_STATUS.md](PROGRESS_STATUS.md)) |
| `app/workers` | ARQ jobs, digest, status cleanup, worker metrics on `:9100` |
| `GET /metrics`, `GET /api/v1/ops/status` | Prometheus and a queue/failure snapshot |

<!-- section: observability -->
## Observability

Prometheus endpoints: API `:8000/metrics`, worker `:9100/metrics`. Grafana, Prometheus and Loki are **not** part of this Compose file; your Prometheus scrapes the endpoints. Import artifacts live in `observability/`. See [OBSERVABILITY.md](OBSERVABILITY.md) and [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

<!-- section: deduplication-and-concurrency -->
## Deduplication and concurrency

The key is `{repository}:{pr_number}:{head_sha}`. A repeated delivery for the same SHA reuses a `pending`, `running` or `completed` run; a `failed` one can be retried. A new SHA cancels the previous run. Manual `POST /api/v1/reviews` defaults to `force: true`, a new run on the same SHA. Different PRs run in parallel (`WORKER_MAX_JOBS`).

Requests for one PR are serialized with a Postgres advisory lock, ARQ job ids are deterministic per run, and the worker claims a run with a conditional update, so concurrent deliveries do not produce duplicate runs. The worker re-reads the PR head before it starts and closes the run as `HEAD_CHANGED` if it moved; for private repositories it checks again before publishing. Details: [API.md](API.md#concurrency-guarantees).

<!-- section: api-authentication -->
## API authentication

`POST /api/v1/reviews`, `GET /api/v1/reviews/{id}` and `GET /api/v1/ops/status` need an API key. `GET /metrics` is for internal scraping; do not expose it. `POST /api/v1/pull-request` accepts only requests with a valid `X-Hub-Signature-256` (no API key); without a valid webhook secret it returns 503, while Actions and the manual API keep working. See [API.md](API.md), [GITHUB_SETUP.md](GITHUB_SETUP.md) and [SECURITY.md](../SECURITY.md).

<!-- section: data-model -->
## Data model

- `repositories` — GitHub full name, enabled, owner, comment authors
- `pull_requests` — number, SHAs, issue key, author, assignee, state, bot title
- `review_runs` — trigger, SHAs, status, versions, duration, error/skip code, progress status
- `findings` — stable id, severity, location, evidence, visibility
- `finding_transitions` — new / resolved / still_open / regressed / obsolete / needs_human
- `task_snapshots` — Jira key and whether a comment/transition was done
- `digest_runs` — digest snapshot

Raw diffs are not stored. More: [DATA_HANDLING.md](DATA_HANDLING.md).

<!-- section: skips-and-errors -->
## Skips and errors

- Fork and draft PRs: `skipped`.
- Author without write access: `skipped`.
- Repository outside a non-empty allowlist: `repo_not_allowed`. An empty list allows any repository.
- Too large a PR: comment `PR_TOO_LARGE_FOR_AI_REVIEW`.
- No Jira key: warning, the code is still reviewed. Jira unavailable: `ISSUE_CONTEXT_UNAVAILABLE`.
- Invalid Codex JSON: one retry, then `AI_OUTPUT_INVALID`.

All codes: [TROUBLESHOOTING.md](TROUBLESHOOTING.md#error-and-skip-codes).

<!-- section: comment-language -->
## Comment language

Output defaults to English. `language.summary` controls the summary and finding titles; `language.details` the scenario, evidence, recommendation and comment labels. Both support `en` and `ru`.

<!-- section: multiple-owners -->
## Multiple owners

`app/owners/` holds the owner model. `OwnerRegistry.build()` reads the env and `config.yaml` once at startup (API and worker), validates everything and builds a frozen `OwnerContext` per active owner: GitHub credentials, webhook secret, API key, Jira and Slack bindings, model key and the owner's effective config. Global settings are read for credentials only there; adapters (`GitHubAppClient.from_credentials`, `JiraClient.from_binding`, `SlackClient.from_binding`, `Publisher.from_context`) take the owner's bindings. A config without `owners:` yields one owner, `default`, whose effective config is the global config (modes A–D: [MULTI_OWNER.md](MULTI_OWNER.md#modes)).

- **Routing.** `resolve()` picks an explicit owner, then an exact `org/repo` claim, an `org/*` claim, the webhook installation id and finally `routing.unclaimed`; the chosen owner's allowlist then applies. The root webhook verifies the signature against every owner's secret and rejects a repo of another owner with `owner_signature_mismatch`; `/api/v1/pull-request/<id>` verifies only that owner's secret.
- **Effective config.** Integrations (`jira`, `slack`) come only from the owner's own blocks. Policy sections (`public_repos`, `language`, `features`, `pr_description`, `size_guard`, `progress_status`, `codex.model`/`reasoning_effort`) are deep-merged over the global sections. The worker, publisher, size guard, PR description, webhook label check and digest read the run owner's effective config.
- **Data.** `repositories`, `review_runs`, `digest_runs` and `findings` carry `owner_id` (migrations `005`-`007`); comment authors are stored with their owner. The worker builds the context from `review_runs.owner_id` and skips runs whose owner was removed, disabled or re-routed (`owner_not_configured`, `owner_disabled`, `owner_changed`). Rows recorded before multi-owner support belong to `default`, or to the owner with `aliases: [default]`.
- **Isolation.** Codex gets only the run owner's model key (`OPENAI_API_KEY`/`CODEX_API_KEY`) in an allowlisted environment; git gets only the owner's GitHub token. The digest runs per owner with its own GitHub credentials, `DigestRun` and Slack channel.
