<!-- section: observability-scrape-our-endpoints-from-your-prometheus-grafana -->
# Observability: scrape our endpoints from *your* Prometheus / Grafana

This service does not run Grafana, Prometheus, or Loki. It only **exports**:

| Export | Where | Auth |
| --- | --- | --- |
| Prometheus text | API `GET /metrics` (`:8000`) | none |
| Prometheus text | Worker `GET /metrics` (`:9100`) | none |
| Ops snapshot | `GET /api/v1/ops/status` | Bearer `REVIEW_API_KEY` |
| Logs | stdout (`LOG_FORMAT=text\|json`) | — |

No observability URLs in `.env`. The platform scraper connects inbound.

<!-- section: prometheus-existing -->
## Prometheus (existing)

Point scrape jobs at the API and worker. Do not publish `/metrics` to the internet.

Merge [observability/prometheus-scrape.yml](../observability/prometheus-scrape.yml) into your Prometheus (or Alloy) config. Hostnames:

- Same Docker network: `api:8000`, `worker:9100`
- Published host ports: `localhost:8000` for the API only. The worker's `:9100` is exposed only on the Docker network, so Prometheus must join that network (or run in it). The bundled worker job uses `dns_sd_configs` on the `worker` service name, which returns one address per replica, so scaled workers are all scraped (Swarm: `tasks.worker`). A static `worker:9100` target would scrape only one replica.
- Kubernetes: Service/Pod scrape / `PodMonitor` / `ServiceMonitor` on those ports

Load [observability/alerts.yml](../observability/alerts.yml) as a rule file (`OpenPrReviewWorkerDown`, `OpenPrReviewWorkerTargetDown`, `OpenPrReviewApiDown`, `OpenPrReviewCodexQuota`, `OpenPrReviewQueueNotDraining`, `OpenPrReviewStaleReviews`). `OpenPrReviewWorkerDown` (`page`) fires when no worker is discovered (`absent(up{job="open-pr-review-worker"})`) or every known worker target is down (`max(up{job="open-pr-review-worker"}) == 0`): reviews queue and never run. The second case is the usual total outage: when all workers stop, the `worker` name stops resolving and Prometheus keeps the last discovered targets at `up == 0`. `OpenPrReviewWorkerTargetDown` (`warning`) fires when a discovered worker replica cannot be scraped (`up == 0`); other replicas may still process reviews. In a total outage it fires alongside `OpenPrReviewWorkerDown`. Limitation: with scaled workers, a replica that stops disappears from Docker DNS discovery instead of reporting `up == 0`, so losing some but not all replicas fires neither alert. Detect partial loss with your Docker or orchestrator metrics (desired vs running replicas); a queue-backlog alert is planned as a follow-up.

<!-- section: dev-prometheus-optional -->
## Dev Prometheus (optional)

For local checks only, [docker-compose.observability.yml](../docker-compose.observability.yml) adds a `prometheus` service on the stack's Docker network. It loads [observability/dev/prometheus.yml](../observability/dev/prometheus.yml), which pulls in the same scrape jobs (`prometheus-scrape.yml`) and alert rules (`alerts.yml`) as above. The UI is published on `127.0.0.1:9090` only: targets at `/targets`, alerts at `/alerts`. There is no Alertmanager, and data lives in `tmpfs`, so it is lost when the container stops. Do not use it in production.

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d prometheus
docker compose up -d --scale worker=2
docker compose -f docker-compose.yml -f docker-compose.observability.yml rm -sf prometheus
```

The first command starts Prometheus, the second scales workers (all replicas show up under `open-pr-review-worker`), the last removes Prometheus. Reload rules after editing `alerts.yml` by restarting the container. CI runs `promtool check config` on this assembled config (same image and mounts as the overlay) and `promtool check rules` on `alerts.yml`.

<!-- section: grafana-existing -->
## Grafana (existing)

1. Datasource Prometheus → the Prometheus that scrapes Open PR Review. Optional Loki for logs.
2. **Dashboards → Import** → [observability/grafana/dashboards/autoreview-bot.json](../observability/grafana/dashboards/autoreview-bot.json).
3. Dashboard expects datasource UIDs `prometheus` and `loki`. Remap if yours differ.

<!-- section: loki-existing -->
## Loki (existing)

Ship container stdout with **your** agent. Compose sets `OPEN_PR_REVIEW_SERVICE=api|worker`. Set `LOG_FORMAT=json` only if that pipeline wants JSON; default `text` is fine for `docker compose logs`.

<!-- section: env-app-not-the-metrics-stack -->
## Env (app, not the metrics stack)

| Variable | Default | Role |
| --- | --- | --- |
| `LOG_FORMAT` | `text` | `json` if your log agent expects JSON |
| `OPEN_PR_REVIEW_SERVICE` | `unknown` | log field `service` (Compose already sets this) |
| `METRICS_PORT` | `9100` | worker bind for `/metrics` |
| `LOG_LEVEL` | `INFO` | |

<!-- section: metrics-that-matter-for-workers -->
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

On-call steps: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
