# Operations and troubleshooting

See [deployment](DEPLOY.md), [observability](OBSERVABILITY.md) and the [Russian translation](ru/OPERATIONS.md).

## How reviews start

The service does not poll GitHub. The current recommended trigger is GitHub Actions in each reviewed repository: copy [the example](../examples/github/ai-review-trigger.yml), set `AI_REVIEW_API_KEY` and `AI_REVIEW_URL`, and add the repository to `github.allowed_repos`. The manual `POST /api/v1/reviews` endpoint is useful for debugging. Direct webhooks await mandatory signature validation. The `ci.yml` in this service repository only runs lint, tests and a Docker build.

Configure `pull_request` event types and branch/path filters in the reviewed repository's workflow. Branch filters apply to the PR base. Draft and fork PRs are skipped by the example. After enabling direct webhooks in the future, use one automatic trigger per repository until concurrent deduplication is implemented.

## First checks

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/is-ready
curl -fsS http://localhost:9100/metrics
docker compose ps
docker compose logs -f worker
```

Query `GET /api/v1/ops/status` with `Authorization: Bearer <REVIEW_API_KEY>` for queue depth, runs, recent failures and diagnostic hints. `health` checks API liveness; `is-ready` checks Postgres and Redis. The worker metric `open_pr_review_arq_queue_jobs` should drain, and `open_pr_review_review_stale_in_flight` should normally be zero. See [observability](OBSERVABILITY.md) for dashboards and alerts.

## Symptoms

| Symptom | Next checks |
| --- | --- |
| API says `202`, no PR comment | Check `ops/status`, worker container/logs, queue depth, allowlist, draft/fork status and author access. |
| Queue grows with no running jobs | Check worker startup and Redis connection. |
| `CODEX_QUOTA` | Verify OpenAI project quota and billing for the worker's API key. Repeated POSTs will only create more failures until quota is available. |
| `CODEX_AUTH` | Verify `OPENAI_API_KEY` is present in the worker environment; recreate worker after changing it. |
| `GIT_FORBIDDEN` or clone `403` | Check PAT/App Contents access and installation; see [GitHub setup](GITHUB_SETUP.md). |
| Run hangs at `codex still running` | Check model timeout, worker logs, outbound access, available memory and Codex subprocess state. |
| Run completed but no comment | Check GitHub write permission, publisher logs and whether the run was reused for the same SHA. |
| Metrics/dashboard absent | Check `:8000/metrics`, `:9100/metrics`, scrape targets and Grafana datasource UID. |
| Logs absent in Loki | The service writes stdout; configure your existing log agent. |

The worker logs include queue/start/finish events and periodic Codex heartbeats. Use a run ID to correlate a failure. Restart API or worker after `config.yaml` changes because configuration is cached. Redis persists queued jobs across ordinary worker restarts; check `ops/status` after recovery. Back up Postgres before schema upgrades.
