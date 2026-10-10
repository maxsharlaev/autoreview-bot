# Deploy Open PR Review

Deploy one self-hosted review service and connect it to GitHub and an existing Prometheus/Grafana stack. See [GitHub setup](GITHUB_SETUP.md), [operations](OPERATIONS.md), [observability](OBSERVABILITY.md), and the [Russian translation](ru/DEPLOY.md).

## Scope and services

The service adds an advisory sticky comment to eligible PRs. It does not install Grafana, Prometheus or Loki, create Jira issues or publish a blocking GitHub check.

| Compose service | Purpose | Host exposure |
| --- | --- | --- |
| `postgres` | Review state | None; Docker network only |
| `redis` | ARQ queue | None; Docker network only |
| `migrate` | One-shot `alembic upgrade head` | None |
| `api` | FastAPI, port 8000 | `127.0.0.1:8000` for a TLS reverse proxy |
| `worker` | Clone, Codex, publication and metrics | `127.0.0.1:9100` for local scrape |

The worker Compose service sets `seccomp:unconfined` and `apparmor:unconfined` so Codex's inner bubblewrap sandbox can start. Do not remove these settings without testing the target host.

## Prerequisites

- Docker Engine and Compose v2; sufficient disk for Postgres and temporary clones. For four concurrent jobs, plan for several vCPUs and at least 8 GB RAM, or lower `WORKER_MAX_JOBS`.
- Outbound HTTPS to GitHub, OpenAI and optional Jira.
- A list of `owner/repo` names for `github.allowed_repos`.
- An OpenAI API key with available quota; a GitHub App installation or fine-grained PAT with repo access; Jira credentials only if its features are enabled.
- A public HTTPS hostname for the Actions trigger. Terminate TLS at nginx, Caddy or ingress. Restrict `/metrics` and worker `:9100` to internal scrapers.

## Configuration

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Generate separate random, URL-safe `POSTGRES_PASSWORD` and `REVIEW_API_KEY` values (for example, `openssl rand -hex 32`). Keep `.env` and `config.yaml` out of Git. Compose derives the database URL for `migrate`, `api` and `worker` from `POSTGRES_PASSWORD`; set `DATABASE_URL` separately for a local Python process.

Set `OPENAI_API_KEY` and either `GITHUB_TOKEN`/`GITHUB_PAT` or `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY`. Set `GITHUB_WEBHOOK_SECRET` if configuring a webhook. For Jira, set `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN`; otherwise disable `features.jira_comment` and `features.jira_transition`. Slack is disabled by default.

Set a non-empty allowlist in `config.yaml`:

```yaml
github:
  allowed_repos:
    - example-org/example-repo
language:
  summary: en
  details: en
features:
  jira_comment: false
  jira_transition: false
```

Both language fields accept `en` or `ru`. English is the default. Restart/recreate API and worker after YAML changes because application configuration is cached.

To supply your own review instructions, copy `prompts/review.md` to `prompts/local/review.md` on the host and set `codex.prompt_file: prompts/local/review.md` in `config.yaml`. Compose mounts `./prompts` read-only into the worker; `prompts/local/` is Git-ignored. The path is relative to `/app` in the container. Keep the output schema and untrusted-input rules in a replacement prompt. Changes to the file take effect on the next review; changing `config.yaml` requires a worker restart. A missing configured file causes the review to fail explicitly.

## Start and verify

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f api worker
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/is-ready
```

Confirm that Postgres and Redis have no published host ports. Proxy only the API through HTTPS. Import the [Prometheus scrape configuration](../observability/prometheus-scrape.yml), [alerts](../observability/alerts.yml) and [Grafana dashboard](../observability/grafana/dashboards/open-pr-review.json) into your existing stack. The default jobs are `open-pr-review-api` and `open-pr-review-worker`.

Use the [example GitHub Actions trigger](../examples/github/ai-review-trigger.yml) in each reviewed repository, with `AI_REVIEW_API_KEY` equal to server `REVIEW_API_KEY` and `AI_REVIEW_URL` set to the HTTPS base URL. Direct webhooks are not recommended yet: the handler currently accepts an API key without a mandatory signature. Never put a secret in the webhook URL.

Open a non-draft PR in an allowed repository. Expect an API `202`, a run in `GET /api/v1/ops/status`, and the `<!-- open-pr-review -->` comment. If the API returns `202` but no comment appears, follow [operations](OPERATIONS.md).

## Several owners

To serve another GitHub organization from the same deployment, add an `owners:` block to `config.yaml` and its `OWNER_<ID>_*` variables to `.env`, then recreate API and worker. Nothing changes until you do: an upgraded deployment without `owners:` keeps the single owner `default` and logs `owner=default`. The [README](../README.md#multiple-owners) describes modes A-D, routing, secret names, per-owner Jira/Slack, policies and model keys, and the step-by-step upgrade and rollback; see [GitHub setup](GITHUB_SETUP.md#several-owners) for connecting the second organization.

Rollback: removing an `owners.<id>` block and restarting stops that owner's work and keeps its rows. Going back to an image from before multi-owner support needs `alembic downgrade 004_finding_details_visibility` with the new image first. The `007_findings_owner_id` downgrade refuses while findings of several owners share a stable id on one pull request; `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1` keeps the repository owner's row (or the oldest) and deletes the others with their transitions. Back up the Postgres volume before any downgrade.

## Ongoing operation

Back up the Postgres volume, rotate keys, and review worker quota and queue metrics. See [operations](OPERATIONS.md) for symptoms and recovery. Reviews remain advisory; Jira comments and transitions follow the feature flags.
