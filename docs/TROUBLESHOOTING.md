# Operations and troubleshooting

The goal of this page: find out quickly **whether the worker is alive and why a review does not appear on a PR**, without reading all of the Codex output. First deployment: [DEPLOY.md](DEPLOY.md). Dashboards and alerts: [OBSERVABILITY.md](OBSERVABILITY.md).

## How reviews start

The service does not poll GitHub. Something has to call it.

| Trigger | When | What to configure |
| --- | --- | --- |
| GitHub Actions in each reviewed repository | Simplest; supports path and branch filters | The workflow below and two repository/org settings. **Not** this repository's `ci.yml`. |
| Direct GitHub webhook | App or repository webhook, signed with `X-Hub-Signature-256` | [GITHUB_SETUP.md](GITHUB_SETUP.md) |
| Manual `POST /api/v1/reviews` | Debugging, scripts | [API.md](API.md) |

This repository's `ci.yml` only runs lint, tests and a Docker build. It does not and should not request reviews.

### GitHub Actions in a reviewed repository

1. Add the repository to `github.allowed_repos` (or the owner's `allowed_repos`), otherwise the API answers `403 repo_not_allowed`.
2. Copy [examples/github/ai-review-trigger.yml](../examples/github/ai-review-trigger.yml) to `.github/workflows/ai-review.yml` in that repository.
3. Set, in the repository or the organization:

   | Name | Kind | Value |
   | --- | --- | --- |
   | `AI_REVIEW_API_KEY` | secret | Same value as `REVIEW_API_KEY` on the server (or the owner's scoped key) |
   | `AI_REVIEW_URL` | variable | `https://review.example.com`, without a trailing `/` |

4. The example uses `maxsharlaev/autoreview-bot@main`. For a fork, or when Actions can't read this repository, change `uses:` or copy the `steps` of [`.github/workflows/ai-review-trigger.yml`](../.github/workflows/ai-review-trigger.yml) into the calling workflow.
5. The job calls `POST /api/v1/reviews` with `pull_url` and `force: false`. No webhook HMAC is involved.

The `on.pull_request` filters decide **which PRs call the service at all**; they are not in the service's `.env`:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
    branches:          # the PR base branch (merge target)
      - main
      - develop
    paths:             # review unless only docs changed
      - "**"
      - "!docs/**"
      - "!**/*.md"
```

- The workflow runs if at least one changed file matches the `paths` patterns.
- Pattern order matters: a later pattern overrides an earlier one. Here `**` matches everything and the `!` patterns then exclude docs, so a docs-only PR is skipped, while a PR touching `src/` and `docs/` still runs.
- GitHub does not allow `paths` and `paths-ignore` for the same event: use one of them, with `!` patterns in `paths` to combine inclusion and exclusion.
- `branches` filters the **base** branch, not the feature branch.
- The example skips drafts with an `if:`; `ready_for_review` runs it after a draft is marked ready.
- The example skips forks (`head.repo != github.repository`).
- `workflow_dispatch` lets you run it by hand with a `pull_url`.

Changing filters is a commit to the reviewed repository; the worker needs no restart. Using both Actions and a direct webhook on one repository is safe (requests for one PR are serialized and the same SHA is reused, see [API.md](API.md#concurrency-guarantees)), but it doubles the calls; prefer one trigger.

## 60 seconds without Grafana

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/is-ready
curl -fsS http://localhost:9100/metrics | grep open_pr_review
docker compose ps
docker compose logs -f worker
```

Queue and recent failures (operator key):

```bash
curl -fsS -H "Authorization: Bearer $REVIEW_API_KEY" http://localhost:8000/api/v1/ops/status
```

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/ops/status" -Headers @{ Authorization = "Bearer $env:REVIEW_API_KEY" }
```

The `hints` field is a plain-language diagnosis: no model quota, no key, jobs queued but none running (the worker does not consume the queue), stale pending/running runs.

## What should be green

| Check | Good | Bad |
| --- | --- | --- |
| `GET /health` | `ok` | API did not start |
| `GET /is-ready` | `ready` | Postgres or Redis unreachable |
| `up{job="open-pr-review-worker"}` | 1 | Worker dead or `:9100` not scraped |
| `open_pr_review_arq_queue_jobs` | 0 or falling | Growing while nothing runs |
| `open_pr_review_review_stale_in_flight` | 0 | A run pending/running for over 15 min |
| Latest `error_code` | empty or a skip | `CODEX_QUOTA`, `CODEX_AUTH`, `GIT_*` |

Worker log lines to look for: `worker ready`, `queued review_run=`, `job start`, `codex start`, `job done`, `review_finished`, `failed: CODEX_QUOTA`. The worker also logs a Codex heartbeat every 20 s. Use the run id to correlate.

## Symptom → action

### API answers `202`, nothing appears on GitHub

1. `GET /api/v1/ops/status`.
2. `queue_jobs > 0` and `in_flight.running = 0`: **the worker does not consume the queue.** Is the container up (`docker compose ps`)? Does the log show `worker ready`, or did it fail at startup (an owner config error stops the worker with exit code 1)? Does `:9100/metrics` answer?
3. `last_failures[].error_code = CODEX_QUOTA`: **no model quota.** The review does not hang; it fails within about 30–40 s. Top up billing for the key's organization. Until then, repeated requests only add failed runs.
4. `CODEX_AUTH`: the key does not reach the sandbox. Check `OPENAI_API_KEY` (or the owner's key) in the **worker** environment, then recreate the worker.
5. `status=completed` right away on the same SHA: the run was reused (webhook, or `force: false`). The manual API defaults to `force: true`; the Actions example sends `force: false`.
6. `GIT_FORBIDDEN` / clone `403`: PAT Contents access or App installation; see [GITHUB_SETUP.md](GITHUB_SETUP.md).
7. `GIT_REF_NOT_FOUND`: the branch was deleted and the SHA or PR ref is gone.
8. The Action does not start: check `on.pull_request.paths` / `branches`, draft, fork. Job runs but the API says `401`: `AI_REVIEW_API_KEY`. `403`: repository not allowed. Check that `AI_REVIEW_URL` has no trailing `/`.
9. A skip: `skip_reason` on `GET /api/v1/reviews/{id}` (draft, fork, author without write access, size guard, owner reasons). Skips publish no full review.

### Worker "hangs" in Codex (heartbeat `codex still running`)

Normal up to `codex.timeout_seconds` (default 600 s). After `WORKER_JOB_TIMEOUT` (default 900 s) ARQ cancels the job and the run becomes `REVIEW_TIMEOUT`. `stale_in_flight > 0` for more than 15 minutes means a run left `running` (for example the process died). Recreate the worker; the next request for that SHA replaces the stale run, and the cleanup job closes its progress status.

### Run `completed`, no sticky comment

Check `skip_reason` / `error_code` on `GET /api/v1/reviews/{id}`. Check Pull requests **write** for the PAT/App and the publisher log lines. A reused run does not post again.

### `pr_queue_busy` (503)

Another request for the same PR held the per-PR lock for 20 s (usually a slow GitHub read or a stuck database). Nothing changed. Retry the request, or redeliver the webhook from the App's or repository's "Recent deliveries".

### No dashboard or no metrics in Grafana

Prometheus must reach `:8000/metrics` and `:9100/metrics` (network, security groups, the scrape job from [`observability/prometheus-scrape.yml`](../observability/prometheus-scrape.yml)). The dashboard is a manual JSON import; alerts from [`observability/alerts.yml`](../observability/alerts.yml) are loaded once into your Prometheus.

### Metrics but no logs in Grafana

The service only writes to stdout; your log agent ships it. Locally: `docker compose logs -f worker`.

## Error and skip codes

| Code | Kind | Meaning |
| --- | --- | --- |
| `draft_pr` | skip | Draft PR. |
| `fork_pr` | skip | PR from a fork. |
| `author_no_write_access` | skip | Author lacks write, maintain or admin permission. |
| `repo_not_allowed` | skip | Repository not in the allowlist. |
| `PR_TOO_LARGE_FOR_AI_REVIEW` | skip / notice | Size guard hard threshold or `codex.max_files` (all changed files, skipped ones included) / `max_diff_lines` exceeded. |
| `owner_not_configured`, `owner_disabled`, `owner_changed` | skip | The run's owner was removed, disabled, or the repository routes elsewhere now. |
| `ISSUE_CONTEXT_UNAVAILABLE` | warning | Jira could not be read; the code review still runs. |
| `AI_OUTPUT_INVALID` | error | Invalid model JSON after one retry. |
| `CODEX_QUOTA` | error | Model quota or billing. Not retried. |
| `CODEX_AUTH` | error | Missing or rejected model key. Not retried. |
| `CODEX_TIMEOUT` | error | One Codex call exceeded `codex.timeout_seconds`. |
| `CODEX_UNAVAILABLE` | error | Codex failed to start or returned an unclassified error. |
| `GIT_FORBIDDEN` | error | Clone denied. |
| `GIT_REF_NOT_FOUND` | error | Commit or ref no longer exists. |
| `GITHUB_UNAVAILABLE` | error | GitHub API or clone failure during the run. |
| `HEAD_CHANGED` | error (status `cancelled`) | The PR head moved before pickup, or before a private repository's review was published. |
| `QUEUE_UNAVAILABLE` | error | Enqueueing to Redis failed. |
| `REVIEW_TIMEOUT` | error | The job exceeded `WORKER_JOB_TIMEOUT`. |
| `INTERNAL_ERROR` | error | Unexpected error, or a run whose job was lost. |

## Restarts and data

| Action | How |
| --- | --- |
| Restart the worker | `docker compose restart worker`; the queue in Redis survives. |
| Apply `.env` / `config.yaml` changes | `docker compose up -d --force-recreate api worker` |
| Backup | The `postgres_data` volume. Redis holds the queue, not the source of truth. |
| Never on production without a backup | `docker compose down -v` deletes the database and the queue. |

Back up Postgres before schema upgrades ([UPGRADE.md](UPGRADE.md)).
