# Architecture

Open PR Review is an advisory service: GitHub PR event → queue → Codex CLI → verified JSON → one updated PR comment. It does not block merges or create Jira issues. See the [roadmap](ROADMAP.md) and [Russian translation](ru/ARCHITECTURE.md).

## Pipeline

```text
GitHub Actions or PR webhook → API key (optional webhook HMAC) → Redis/ARQ
  → worker → GitHub PR and diff → optional Jira issue snapshot
  → bounded, untrusted-marked prompt → clone at head SHA
  → Codex CLI in read-only sandbox → JSON verifier
  → Postgres runs/findings/transitions → sticky GitHub comment
  → optional Jira comment/status transition and Slack notification
Scheduler → digest of open PRs → optional Slack message
```

## Components

| Component | Responsibility |
| --- | --- |
| `app/api/v1/pull_request.py` | Webhook ingress, sequential deduplication, cancellation request for an older SHA |
| `app/adapters/github.py` | GitHub App/PAT access, PRs, diffs and comments |
| `app/adapters/jira.py` | Issue read, comment and status transition |
| `app/adapters/slack.py` | Optional notifications; disabled by default |
| `app/services/orchestrator.py` | One review run |
| `app/services/codex_runner.py` | Codex subprocess with restricted environment |
| `app/services/verifier.py` | Schema, finding location and count validation |
| `app/services/publisher.py` | GitHub, Jira and Slack publication |
| `app/workers` | ARQ jobs, digest and worker metrics |

## State and deduplication

The sequential deduplication key is `{repository}:{pr_number}:{head_sha}`. A repeated webhook can reuse a pending, running or completed run. A new SHA requests cancellation of the previous job. Manual reviews default to `force: true`. Atomic deduplication and a final head SHA check before publication remain [roadmap work](ROADMAP.md).

Postgres stores repositories, pull requests, review runs, findings, finding transitions, Jira task snapshots and digest runs. Raw PR diffs are not stored in the database.

## Security and failure handling

The webhook, diff, PR fields and Jira text are untrusted. `REVIEW_API_KEY` protects review and operations endpoints. Webhook HMAC is checked when present, but is not currently mandatory; use the Actions workflow or manual API for public deployments. `/metrics` is intended for internal scraping. See [GitHub setup](GITHUB_SETUP.md).

Forks, drafts and unauthorized authors are skipped. An empty repository allowlist currently allows all repos. Large PRs produce `PR_TOO_LARGE_FOR_AI_REVIEW`; unavailable Jira context produces a warning; invalid model JSON gets one retry before `AI_OUTPUT_INVALID`.

Prometheus endpoints are API `:8000/metrics` and worker `:9100/metrics`. See [observability](OBSERVABILITY.md) and [operations](OPERATIONS.md).

Output defaults to English. `language.summary` controls summary and finding titles; `language.details` controls finding details and comment labels. Both currently support `en` and `ru`.

## Multiple owners

`app/owners/` holds the owner model. `OwnerRegistry.build()` reads the env and `config.yaml` once at startup (API and worker), validates everything and builds a frozen `OwnerContext` per active owner: GitHub credentials, webhook secret, API key, Jira and Slack bindings, model key and the owner's effective config. Global settings are read for credentials only there; adapters (`GitHubAppClient.from_credentials`, `JiraClient.from_binding`, `SlackClient.from_binding`, `Publisher.from_context`) take the owner's bindings. A config without `owners:` yields one owner, `default`, whose effective config is the global config (modes A-D are described in the [README](../README.md#multiple-owners)).

- **Routing.** `resolve()` picks an explicit owner, then an exact `org/repo` claim, an `org/*` claim, the webhook installation id and finally `routing.unclaimed`; the chosen owner's allowlist then applies. The root webhook verifies the signature against every owner's secret and rejects a repo of another owner with `owner_signature_mismatch`; `/api/v1/pull-request/<id>` verifies only that owner's secret.
- **Effective config.** Integrations (`jira`, `slack`) come only from the owner's own blocks. Policy sections (`public_repos`, `language`, `features`, `pr_description`, `size_guard`, `codex.model`/`reasoning_effort`) are deep-merged over the global sections. The worker, publisher, size guard, PR description, webhook label check and digest read the run owner's effective config.
- **Data.** `repositories`, `review_runs`, `digest_runs` and `findings` carry `owner_id` (migrations `005`-`007`); comment authors are stored with their owner. The worker builds the context from `review_runs.owner_id` and skips runs whose owner was removed, disabled or re-routed (`owner_not_configured`, `owner_disabled`, `owner_changed`). Rows recorded before multi-owner support belong to `default`, or to the owner with `aliases: [default]`.
- **Isolation.** Codex gets only the run owner's model key (`OPENAI_API_KEY`/`CODEX_API_KEY`) in an allowlisted environment; git gets only the owner's GitHub token. The digest runs per owner with its own GitHub credentials, `DigestRun` and Slack channel.
