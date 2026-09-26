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
