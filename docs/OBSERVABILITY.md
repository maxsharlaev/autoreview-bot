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

<!-- section: dev-prometheus-and-grafana-optional -->
## Dev Prometheus, Grafana and Loki (optional)

For local checks only, [docker-compose.observability.yml](../docker-compose.observability.yml) adds `prometheus`, `grafana`, `loki`, `alloy` and `docker-socket-proxy` services on the stack's Docker network. Do not use it in production. There is no Alertmanager, and data lives in `tmpfs`, so it is lost when a container stops. Prometheus joins both `default` and `internal`, because api/worker are attached to `internal` only.


```bash
# Full stack with Prometheus, Grafana and Loki
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --build
# Stop everything (Postgres data is kept)
docker compose -f docker-compose.yml -f docker-compose.observability.yml down
```

<!-- section: dev-prometheus -->
### Prometheus

[observability/dev/prometheus.yml](../observability/dev/prometheus.yml) pulls in the same scrape jobs (`prometheus-scrape.yml`) and alert rules (`alerts.yml`) as above. The UI is on `http://127.0.0.1:9090` only: targets at `/targets`, alerts at `/alerts`. Reload rules after editing `alerts.yml` by restarting the container. CI runs `promtool check config` on this assembled config (same image and mounts as the overlay) and `promtool check rules` on `alerts.yml`.

<!-- section: dev-grafana -->
### Grafana

Grafana is on `http://127.0.0.1:3000` only. It provisions a `Prometheus` datasource (uid `prometheus`, `http://prometheus:9090`) from [observability/dev/grafana/provisioning](../observability/dev/grafana/provisioning) and loads the bundled dashboard [observability/grafana/dashboards/autoreview-bot.json](../observability/grafana/dashboards/autoreview-bot.json) as the home page. The provisioned dashboard is read-only, and anonymous users can view it. Signing in as `admin` with the password from `GRAFANA_ADMIN_PASSWORD` (default `admin`, local only) lets you use **Save as** to make a copy for experiments; the copy lasts until the container restarts. Make permanent changes in `observability/grafana/dashboards/autoreview-bot.json`. It also provisions a `Loki` datasource (uid `loki`, `http://loki:3100`) for the dashboard's logs panel.

<!-- section: dev-loki-and-alloy -->
### Loki and Alloy

Loki ([observability/dev/loki.yml](../observability/dev/loki.yml)) runs as a single binary with no host port; Grafana reaches it at `http://loki:3100`. Storage is a 512m tmpfs, and the compactor enforces 24h retention. Alloy ([observability/dev/alloy.alloy](../observability/dev/alloy.alloy)) reads the stdout and stderr of this Compose project's containers through a pinned `wollomatic/socket-proxy` on an internal network (no host ports) and pushes them to Loki with the labels `service` (Compose service name, e.g. `worker`, `api`) and `container`. Explore example: `{service="worker"}`.

The logs contain exactly what `docker compose logs` shows, nothing more, so treat them like those logs: dev only, and do not expose Loki or Grafana beyond `127.0.0.1`. Mounting the Docker socket `:ro` is not an API boundary: only the proxy mounts it. The proxy allowlist permits only GET list/inspect/logs and networks (plus ping/version); archive, export, create, and exec are denied. Alloy filters to this Compose project (the name Compose derives from the directory or `-p` / `COMPOSE_PROJECT_NAME`). On the first start Loki rejects lines older than 7 days from long-running containers, which shows up as `timestamp too old` errors in its log and is harmless.

> **Warning (accepted risk):** the Compose project filter lives only in Alloy's config. A compromised Alloy can call inspect and logs for **every** container on the host through the proxy, including environment variables and secrets of unrelated containers. Do not run this logs overlay on a machine that hosts sensitive containers outside this project. Dev only.

<!-- section: dev-prometheus-only-and-scaling -->
### Prometheus only and scaling

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d prometheus
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --scale worker=2 worker
docker compose -f docker-compose.yml -f docker-compose.observability.yml rm -sf prometheus grafana loki alloy docker-socket-proxy
```

The first command starts only Prometheus, the second scales workers (all replicas show up under `open-pr-review-worker`), the last removes the observability services.

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

`open_pr_review_arq_queue_jobs`, `open_pr_review_review_in_flight`, `open_pr_review_review_stale_in_flight`, and `open_pr_review_review_recent_failures` are global (shared Redis / Postgres). The API and every worker export the same values, so the bundled dashboard and alerts wrap them in `max(...)` (or `max by (error_code)`) to show one number and fire one alert.

`CODEX_QUOTA` while you retry PRs is empty OpenAI credits: API still returns 202, the worker fails fast. That is not a hang.

On-call steps: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
