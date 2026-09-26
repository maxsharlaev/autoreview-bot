# Observability: scrape our endpoints from *your* Prometheus / Grafana

This service does not run Grafana, Prometheus, or Loki. It only **exports**:

| Export | Where | Auth |
| --- | --- | --- |
| Prometheus text | API `GET /metrics` (`:8000`) | none |
| Prometheus text | Worker `GET /metrics` (`:9100`) | none |
| Ops snapshot | `GET /api/v1/ops/status` | Bearer `REVIEW_API_KEY` |
| Logs | stdout (`LOG_FORMAT=text\|json`) | — |

No observability URLs in `.env`. The platform scraper connects inbound.

## Prometheus (existing)

Point scrape jobs at the API and worker. Do not publish `/metrics` to the internet.

Merge [observability/prometheus-scrape.yml](../observability/prometheus-scrape.yml) into your Prometheus (or Alloy) config. Hostnames:

- Same Docker network: `api:8000`, `worker:9100`
- Published host ports: `localhost:8000`, `localhost:9100` (or the VM IP)
- Kubernetes: Service/Pod scrape / `PodMonitor` / `ServiceMonitor` on those ports

Load [observability/alerts.yml](../observability/alerts.yml) as a rule file (`OpenPrReviewWorkerDown`, `OpenPrReviewCodexQuota`, `OpenPrReviewQueueNotDraining`, `OpenPrReviewStaleReviews`).

## Grafana (existing)

1. Datasource Prometheus → the Prometheus that scrapes Open PR Review. Optional Loki for logs.
2. **Dashboards → Import** → [observability/grafana/dashboards/autoreview-bot.json](../observability/grafana/dashboards/autoreview-bot.json).
3. Dashboard expects datasource UIDs `prometheus` and `loki`. Remap if yours differ.

## Loki (existing)

Ship container stdout with **your** agent. Compose sets `OPEN_PR_REVIEW_SERVICE=api|worker`. Set `LOG_FORMAT=json` only if that pipeline wants JSON; default `text` is fine for `docker compose logs`.

## Env (app, not the metrics stack)

| Variable | Default | Role |
| --- | --- | --- |
| `LOG_FORMAT` | `text` | `json` if your log agent expects JSON |
| `OPEN_PR_REVIEW_SERVICE` | `unknown` | log field `service` (Compose already sets this) |
| `METRICS_PORT` | `9100` | worker bind for `/metrics` |
| `LOG_LEVEL` | `INFO` | |

## Metrics that matter for workers

| Metric | Meaning |
| --- | --- |
| `up{job="open-pr-review-worker"}` | Worker process is scraped |
| `open_pr_review_worker_jobs_in_progress` | Jobs on this process now |
| `open_pr_review_arq_queue_jobs` | ARQ waiting list |
| `open_pr_review_review_in_flight{status="pending\|running"}` | Unfinished `review_runs` |
| `open_pr_review_review_stale_in_flight` | pending/running older than 15m |
| `open_pr_review_review_runs_total{status,error_code,trigger}` | Finished runs |
| `open_pr_review_review_recent_failures{error_code}` | Failures in the last hour |
| `open_pr_review_codex_attempts_total{result}` | `ok`, `CODEX_QUOTA`, `CODEX_AUTH`, … |
| `open_pr_review_review_enqueue_total{result}` | `queued` vs `reused` |

`CODEX_QUOTA` while you retry PRs is empty OpenAI credits: API still returns 202, the worker fails fast. That is not a hang.

On-call steps: [OPERATIONS.md](OPERATIONS.md).
