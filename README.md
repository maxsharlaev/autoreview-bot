# autoreview-bot

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![CI](https://github.com/maxsharlaev/autoreview-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/maxsharlaev/autoreview-bot/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Self-hosted FastAPI service for advisory pull request reviews. It reads GitHub PRs, runs the Codex CLI in a read-only sandbox, records findings, and keeps one sticky PR comment up to date. It can read a linked Jira issue, comment on it and move it to a rework status; Slack notifications and PR description drafts are optional.

**Status:** early MVP. Reviews never block merges. The code has unit tests, Postgres migration tests and CI; the full GitHub → worker → Jira path is not yet covered by automated end-to-end tests. See the [roadmap](docs/ROADMAP.md).

**Supported now:** GitHub (App or PAT), Codex CLI, Jira, optional Slack, several GitHub owners in one deployment. Other code hosts, issue trackers and reviewer CLIs are planned.

## What it does

1. A GitHub Actions job (`POST /api/v1/reviews`, API key) or a signed GitHub webhook (`POST /api/v1/pull-request`, `X-Hub-Signature-256`) requests a review on `opened`, `synchronize`, `reopened` and `ready_for_review`.
2. The service checks the repository allowlist and skips forks, drafts and authors without write access.
3. Requests for the same `{repo}:{pr}:{head_sha}` reuse one run; a new SHA cancels the previous one; concurrent requests for one PR are serialized. Different PRs run in parallel (`WORKER_MAX_JOBS`, default 4).
4. The worker loads the diff and an optional Jira issue, builds an untrusted-marked prompt, clones the head SHA and runs `codex exec --sandbox read-only`.
5. It validates the JSON output, stores findings and their transitions, updates the sticky comment (`<!-- open-pr-review -->`) and a progress check run / commit status.
6. Optionally: Jira comment and transition to `rework_status` on P0/P1 findings, a PR title/description draft, and a scheduled digest of open PRs (Slack only if enabled).

## Quick start

```bash
cp .env.example .env
cp config.example.yaml config.yaml
# .env: POSTGRES_PASSWORD (URL-safe random), REVIEW_API_KEY, GITHUB_TOKEN or the App fields, OPENAI_API_KEY
# config.yaml: github.allowed_repos; disable Jira features unless Jira is configured
docker compose up --build
curl -fsS http://localhost:8000/is-ready
```

Start a first review by hand:

```bash
curl -sS -X POST http://localhost:8000/api/v1/reviews \
  -H "Authorization: Bearer $REVIEW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"pull_url":"https://github.com/example-org/example-repo/pull/1"}'
```

Expect `202` with a `review_run_id`, then a sticky comment on the PR. For automatic reviews add the [example Actions workflow](examples/github/ai-review-trigger.yml) to the reviewed repository or configure a webhook ([GitHub setup](docs/GITHUB_SETUP.md)). Local development without Docker: [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

| Topic | Document |
| --- | --- |
| Index of all docs | [docs/README.md](docs/README.md) |
| Deploy to a server | [docs/DEPLOY.md](docs/DEPLOY.md) |
| GitHub App, PAT, webhook, Actions | [docs/GITHUB_SETUP.md](docs/GITHUB_SETUP.md) |
| Every `config.yaml` key | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Every environment variable | [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) |
| Several GitHub owners | [docs/MULTI_OWNER.md](docs/MULTI_OWNER.md) |
| HTTP API and error codes | [docs/API.md](docs/API.md) |
| Troubleshooting and operations | [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) |
| Upgrades and migrations | [docs/UPGRADE.md](docs/UPGRADE.md) |
| Security policy and threat model | [SECURITY.md](SECURITY.md) |
| What data goes where | [docs/DATA_HANDLING.md](docs/DATA_HANDLING.md) |
| FAQ | [docs/FAQ.md](docs/FAQ.md) |
| Russian documentation | [docs/ru/](docs/ru/README.md) |

Licensed under [Apache-2.0](LICENSE). Copyright 2026 Maksim Sharlaev.
