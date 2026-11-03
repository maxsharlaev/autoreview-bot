<!-- section: deploy-autoreview-bot -->
# Deploy autoreview-bot

Goal: run **one** instance of the review service and connect it to GitHub and an existing Prometheus/Grafana stack. Grafana, Prometheus and Loki are **not** installed by this service.

Related: [GITHUB_SETUP.md](GITHUB_SETUP.md), [CONFIGURATION.md](CONFIGURATION.md), [ENVIRONMENT.md](ENVIRONMENT.md), [TROUBLESHOOTING.md](TROUBLESHOOTING.md), [OBSERVABILITY.md](OBSERVABILITY.md), [SECURITY.md](../SECURITY.md), [Russian translation](ru/DEPLOY.md).

<!-- section: outcome -->
## Outcome

A Compose stack runs on an internal host (VM or Docker host). An open PR in an allowlisted repository gets a sticky comment. The worker is visible in Grafana through metric scraping.

<!-- section: out-of-scope -->
## Out of scope

- Installing Grafana, Prometheus or Loki from this repository.
- Shipping logs to Loki from the application (stdout only; your agent ships it).
- A blocking GitHub check (the progress status is informational, [PROGRESS_STATUS.md](PROGRESS_STATUS.md)).
- Creating Jira issues (the service only comments and transitions, if enabled).

<!-- section: what-is-deployed -->
## What is deployed

Compose from the repository root (`docker-compose.yml`):

| Service | Purpose | Port inside | Expose |
| --- | --- | --- | --- |
| `postgres` | Review state | 5432 | **No** (Docker network only) |
| `redis` | ARQ queue | 6379 | **No** |
| `migrate` | One-shot `alembic upgrade head` | — | No |
| `api` | FastAPI | 8000 | `127.0.0.1:8000` → HTTPS reverse proxy |
| `worker` | Clone, Codex, publication, metrics | 9100 | **No**: Docker network only (`expose`), so replicas can scale |

Compose publishes no Postgres or Redis ports. The API is published on `127.0.0.1:8000` only, and worker metrics are not published at all; use a reverse proxy or a private network for external HTTPS and a remote Prometheus.

The worker **must** run with `security_opt: seccomp:unconfined` and `apparmor:unconfined` (already in Compose) so Codex's inner bubblewrap sandbox can start. Do not remove them without testing on the target host.

<!-- section: what-to-collect-first -->
## What to collect first

| Item | Why |
| --- | --- |
| This repository (the branch to deploy) | Compose and `observability/` |
| List of `owner/repo` to review | `github.allowed_repos` in `config.yaml` |
| `OPENAI_API_KEY` and confirmed **credits** on its organization | Codex. Without credits a review fails with `CODEX_QUOTA` in about 30 s |
| GitHub: a fine-grained PAT **or** permission to create a GitHub App | Clone and PR comment ([GITHUB_SETUP.md](GITHUB_SETUP.md)) |
| Jira, if needed: URL, service account email, API token, project → `rework_status` | Otherwise disable `jira_comment` / `jira_transition` |
| A public HTTPS hostname that GitHub/Actions can reach | For example `https://review.example.com` |
| Where metrics are scraped from | API: IP/DNS of `api:8000` as seen from Prometheus; worker: `worker:9100` from the stack's Docker network only (DNS discovery of replicas) |

<!-- section: host-resources -->
## Host resources

- Docker Engine and Compose v2.
- Outbound HTTPS to `github.com`, `api.github.com`, `api.openai.com` (and Jira if enabled).
- Disk: the `postgres_data` volume plus temporary clones on the worker (hundreds of MB per job).
- CPU/RAM: Codex is heavy. For 4 parallel jobs (`WORKER_MAX_JOBS=4`) plan several vCPUs and at least 8 GB RAM, or lower the parallelism.

<!-- section: network-and-tls -->
## Network and TLS

1. From the internet/GitHub, expose **only** the API over HTTPS. Terminate TLS at nginx, Caddy or an ingress.
2. Proxy `/` → `api:8000`. Don't expose `/metrics` (block the location or use an ACL). Consider blocking `/docs` and `/openapi.json` as well; they are unauthenticated.
3. `GET /health` and `GET /is-ready` need no secret and can stay open for probes.
4. Worker `:9100`: Prometheus/internal network only.
5. GitHub webhooks and Actions call `https://<host>/api/v1/…`. GitHub does not accept self-signed certificates.

<!-- section: configuration-on-the-host -->
## Configuration on the host

```bash
cp .env.example .env
cp config.example.yaml config.yaml
# fill in, never commit
```

<!-- section: env-secrets -->
### `.env` (secrets)

Generate `POSTGRES_PASSWORD` and `REVIEW_API_KEY` (`openssl rand -hex 32` each). The Postgres password must be URL-safe because Compose puts it into `DATABASE_URL`. `REVIEW_API_KEY` is **not** a GitHub PAT.

Required:

```bash
POSTGRES_PASSWORD=<generated hex>
REVIEW_API_KEY=<generated key>
OPENAI_API_KEY=sk-...          # also passed as CODEX_API_KEY inside the sandbox
GITHUB_TOKEN=github_pat_...    # or the App fields below
WORKER_MAX_JOBS=4
WORKER_JOB_TIMEOUT=900
LOG_LEVEL=INFO
LOG_FORMAT=text                # json only if your log agent expects JSON
METRICS_PORT=9100
```

GitHub App instead of a PAT: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, optionally `GITHUB_INSTALLATION_ID=0`. `GITHUB_WEBHOOK_SECRET` if you use webhooks.

Jira: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`. Slack is off by default (`SLACK_BOT_TOKEN` empty, `slack.enabled: false`).

Compose builds `DATABASE_URL` for `migrate`, `api` and `worker` from `POSTGRES_PASSWORD`; `REDIS_URL` stays internal. For a local Python process set `DATABASE_URL` yourself. Every variable: [ENVIRONMENT.md](ENVIRONMENT.md).

<!-- section: config-yaml -->
### `config.yaml`

```yaml
github:
  allowed_repos:
    - example-org/example-repo
language:
  summary: en
  details: en
features:
  jira_comment: false    # true when Jira is connected
  jira_transition: false
  digest_enabled: true
```

Set a non-empty allowlist: an empty list allows any repository the credentials can reach. Both language fields accept `en` or `ru`. Recreate `api` and `worker` after YAML changes; configuration is cached. Every key: [CONFIGURATION.md](CONFIGURATION.md).

Custom review instructions: copy `prompts/review.md` to `prompts/local/review.md`, edit the copy and set `codex.prompt_file: prompts/local/review.md`. `prompts/local/` is git-ignored; Compose mounts `./prompts` read-only into the worker, and the path is relative to `/app` in the container. Keep the output schema and untrusted-input rules in a replacement prompt. A missing file fails the review explicitly. File edits apply to the next review; changing the path needs a worker restart.

<!-- section: compose-scripts -->
## Compose helper scripts

Preferred local commands (from the repo root):

```powershell
.\scripts\stack.ps1 up
.\scripts\stack.ps1 -Observability up
.\scripts\stack.ps1 -InProcess up
.\scripts\stack.ps1 compose -- config
.\scripts\stack.ps1 compose config
.\scripts\stack.ps1 status
.\scripts\stack.ps1 down
```

```bash
./scripts/stack.sh up
./scripts/stack.sh --observability up
./scripts/stack.sh --in-process up
./scripts/stack.sh compose config
./scripts/stack.sh compose -- config
./scripts/stack.sh status
./scripts/stack.sh down
```

Since M4, `up` **includes** `docker-compose.container-runner.yml` by default (launcher + model-proxy), builds the `review-image` service (`--profile build-review`, tag `${OPR_REVIEW_IMAGE:-opr-review:local}`), and **requires** `LAUNCHER_TOKEN` (≥ 16 chars) in the environment or `.env` (the script prints how to generate one; it never prints the value). `status` / `down` / `restart` / `logs` still use that overlay when not `-InProcess`, but do **not** re-check `LAUNCHER_TOKEN`. Opt out with `-InProcess` / `--in-process` and set `codex.runner_backend: in_process`. `-Container` / `--container` remain accepted no-ops for back-compat. `compose` passthrough (`./scripts/stack.sh compose …` / `.\scripts\stack.ps1 compose -- …`) uses the same `-f` files without token/image build; an optional leading `--` after `compose` is consumed (not forwarded to `docker compose`). Raw `docker compose -f …` remains a non-helper alternative.

<!-- section: deployment-steps -->
## Deployment steps

1. Clone the repository on the host; create `.env` and `config.yaml` as above.
2. Check that Postgres/Redis are not published on the host, `8000` is reachable by the reverse proxy over loopback, and `9100` only inside the Docker network.
3. Default backend is `codex.runner_backend: container` (M4). Set `LAUNCHER_TOKEN` and `PROXY_ISSUER_TOKEN` in `.env`, create `model-proxy.env` from `model-proxy.env.example` (restricted perms; never mount on worker/launcher), then `./scripts/stack.ps1 up` (or `./scripts/stack.sh up`) — the helper runs `docker compose … --profile build-review build review-image` (tag `${OPR_REVIEW_IMAGE:-opr-review:local}`) and includes the container-runner overlay by default. Wait for `migrate` (exit 0), then `api`, `worker`, `launcher`, and `model-proxy` Up. Manual / non-helper deploys still need `docker compose -f docker-compose.yml -f docker-compose.container-runner.yml --profile build-review build review-image` before `docker compose -f docker-compose.yml -f docker-compose.container-runner.yml up -d --build`. For the explicit `in_process` fallback: set `codex.runner_backend: in_process` and `./scripts/stack.ps1 -InProcess up` (or `./scripts/stack.sh --in-process up`). Restrict the host firewall / cloud security group for Compose `proxy_egress` to the configured upstream model host (`PROXY_UPSTREAM_HOST`). Upgrades of worker, launcher, and model-proxy must be drained and applied together — see [UPGRADE.md](UPGRADE.md#container-backend--model-proxy-m3-no-rolling-mix).

4. Checks from the host or internal network:

   ```bash
   curl -sS https://review.example.com/health
   curl -sS https://review.example.com/is-ready    # "ready", otherwise Postgres/Redis
   ./scripts/stack.sh compose exec worker python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:9100/metrics').read().decode()[:200])"
   ```

5. Reverse proxy and TLS. Check that `/metrics` is **not** reachable from outside.
6. Connect GitHub (below).
7. Connect Prometheus/Grafana.
8. Smoke test on a real PR.

<!-- section: how-events-reach-the-service -->
## How events reach the service

The service does **not** poll repositories. Choose one automatic trigger per repository.

<!-- section: option-a-github-actions-in-each-reviewed-repository -->
### Option A: GitHub Actions in each reviewed repository

Simplest; supports `paths` / `branches` filters; works with a PAT or an App.

1. Add the repository to `allowed_repos`.
2. Org/repo secret `AI_REVIEW_API_KEY` = `REVIEW_API_KEY` (or the owner's scoped key).
3. Variable `AI_REVIEW_URL` = `https://review.example.com` (no trailing `/`).
4. Copy [examples/github/ai-review-trigger.yml](../examples/github/ai-review-trigger.yml) to `.github/workflows/ai-review.yml` in the reviewed repository.
5. The example calls `maxsharlaev/autoreview-bot/.github/workflows/ai-review-trigger.yml@main`. If Actions can't see this repository, paste the `steps` of `.github/workflows/ai-review-trigger.yml` instead of `uses:`.
6. Adjust `on.pull_request.branches` and either `paths` or `paths-ignore`, not both ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#github-actions-in-a-reviewed-repository)).

This repository's `ci.yml` does not run reviews, only the service's tests. Action filters and the service allowlist are separate layers: a green API with no Action run means checking `paths`/`branches`/draft.

<!-- section: option-b-direct-webhook -->
### Option B: direct webhook

A GitHub App (or repository) webhook to `/api/v1/pull-request`. Every delivery must be signed with `GITHUB_WEBHOOK_SECRET` (`X-Hub-Signature-256`). Never put a secret in the URL. Checklist: [GITHUB_SETUP.md](GITHUB_SETUP.md).

Running both triggers on one repository does not create duplicate runs for one SHA (see [API.md](API.md#concurrency-guarantees)) but doubles the requests.

<!-- section: observability-your-stack -->
## Observability (your stack)

The application does **not** push metrics or logs; your scraper pulls.

1. Prometheus: merge [observability/prometheus-scrape.yml](../observability/prometheus-scrape.yml). API: `api:8000` (or host/IP when scraping from outside the Docker network). Worker: Prometheus must be on the stack's Docker network (or have explicit routing to it); the worker job finds every replica through `dns_sd_configs` on the `worker` service name. To scrape the worker from the host, add a `docker-compose.override.yml` with `ports: ["127.0.0.1:9100:9100"]` on `worker` (see [UPGRADE.md](UPGRADE.md)); the worker can't be scaled then. Keep the job names `open-pr-review-api` / `open-pr-review-worker`.
2. Rules: [observability/alerts.yml](../observability/alerts.yml): `OpenPrReviewWorkerDown`, `OpenPrReviewWorkerTargetDown`, `OpenPrReviewApiDown`, `OpenPrReviewCodexQuota`, `OpenPrReviewQueueNotDraining`, `OpenPrReviewStaleReviews`.
3. Grafana: import [observability/grafana/dashboards/autoreview-bot.json](../observability/grafana/dashboards/autoreview-bot.json). Default datasource UIDs are `prometheus` and `loki`; point them to yours.
4. Logs: your agent (Promtail/Alloy) reads stdout of `api` and `worker`. Compose sets `OPEN_PR_REVIEW_SERVICE` (`api` / `worker`), which appears as the `service` field in JSON logs (`LOG_FORMAT=json`). There is no Loki client in the service; the dashboard's log panel stays empty until your agent ships to Loki.

`GET /api/v1/ops/status` (Bearer `REVIEW_API_KEY`) gives a queue snapshot without Grafana.

<!-- section: acceptance-criteria -->
## Acceptance criteria

- [ ] `GET /health` and `GET /is-ready` answer on the service hostname.
- [ ] `/metrics` and the Postgres/Redis ports are not reachable from outside.
- [ ] Prometheus scrapes api and worker (`up{job="open-pr-review-worker"} == 1`).
- [ ] Dashboard imported; alerts loaded into Prometheus.
- [ ] On a test **open, non-draft** PR from the allowlist:
  - worker log: `worker ready` → `job start` → `codex start` → `job done` / `review_finished`;
  - the PR has a sticky comment with the `<!-- open-pr-review -->` marker;
  - `GET /api/v1/ops/status` shows no `CODEX_QUOTA` / `CODEX_AUTH` among recent failures.
- [ ] Repeating the same SHA reuses the run (webhook, or Action with `force: false`).
- [ ] `./scripts/stack.sh restart worker` / `.\scripts\stack.ps1 restart worker` keeps the queue. Never run `./scripts/stack.sh down -v` / `.\scripts\stack.ps1 down -v` in production without a volume backup.
- [ ] The [hardening checklist](../SECURITY.md#deployment-hardening-checklist) is done.

API `202` but no comment: [TROUBLESHOOTING.md](TROUBLESHOOTING.md) (`ops/status`, model quota, PAT Contents, allowlist).

<!-- section: several-owners -->
## Several owners

To serve another GitHub organization from the same deployment, add an `owners:` block to `config.yaml` and its `OWNER_<ID>_*` variables to `.env`, then recreate `api` and `worker`. Nothing changes until you do: a deployment without `owners:` keeps the single owner `default` and logs `owner=default`. Everything else: [MULTI_OWNER.md](MULTI_OWNER.md).

<!-- section: manual-real-model-e2e -->
## Manual real-model E2E (opt-in)

Happy-path check that a review through the **container** backend + **model-proxy** completes with a real upstream model. Skipped unless you set `OPR_REAL_MODEL_E2E=1`. **Never runs in CI** (forced skip when a standard CI indicator is set — `CI`, `GITHUB_ACTIONS`, `JENKINS_URL`, `TF_BUILD`, `TEAMCITY_VERSION`, `GITLAB_CI`, `BUILDKITE`, `CIRCLECI` — even with the flag; CI stays green without any model key).

<!-- section: real-model-e2e-prerequisites -->
### Prerequisites

- Docker Engine available to the host (same as local `stack up`).
- A **separate low-limit** OpenAI-issued API key used **only** for this test (the E2E hard-codes `PROXY_PROVIDER=openai` and `api.openai.com`; only one upstream provider is implemented).
- Put that key in `model-proxy.env` as `OPENAI_API_KEY=...` (gitignored; copy from `model-proxy.env.example`). The test reads **only** that file (ambient `OPENAI_API_KEY` is ignored). Do **not** put the key in `.env` for this test path, and never commit it.
- Optional: `OPR_REAL_MODEL` (default `gpt-4o-mini`) to pick a cheap chat model.

<!-- section: real-model-e2e-how-to-run -->
### How to run

From the repo root (PowerShell):

```powershell
$env:OPR_REAL_MODEL_E2E = "1"
# key must be in model-proxy.env only (ambient OPENAI_API_KEY ignored)
python -m pytest tests/test_real_model_e2e.py -q
```

Bash:

```bash
# key must be in model-proxy.env only (ambient OPENAI_API_KEY ignored)
OPR_REAL_MODEL_E2E=1 python -m pytest tests/test_real_model_e2e.py -q
```

Without the flag the test is collected and skipped (safe for CI / full suite).

Only **one** real-model E2E run per host at a time (OS exclusive lock; a second run fails fast). If a run crashes, leftover containers labeled `opr.e2e=1` are removed on the next run after the lock is acquired; or remove the listed containers manually. Review containers are scoped by `opr.job_id` (the launcher does not set `opr.e2e`).

<!-- section: real-model-e2e-expected-cost -->
### Expected cost

One short review against a small fixture (default `gpt-4o-mini`, low reasoning). Expect on the order of **a few cents** per run; use a key with a hard spend cap.

<!-- section: real-model-e2e-what-it-checks -->
### What it checks

- Launcher + model-proxy + real `Dockerfile.review` image path completes with `status=ok` and schema-valid JSON.
- Review container env has `CODEX_API_KEY` starting with `opr_job_` and **no** `OPENAI_API_KEY` / upstream `sk-` value in the launch spec.
- Proxy / launcher logs and model-proxy `/metrics` do **not** contain the upstream key value (asserted without printing it).

No TTL / negative-path coverage here — happy path only.

<!-- section: after-rollout -->
## After rollout

| Action | How |
| --- | --- |
| Restart the worker | `./scripts/stack.sh restart worker` / `.\scripts\stack.ps1 restart worker`; the Redis queue survives |
| Change `.env` / `config.yaml` | `./scripts/stack.sh compose up -d --force-recreate api worker` / `.\scripts\stack.ps1 compose -- up -d --force-recreate api worker` |
| Backup | The `postgres_data` volume; Redis is the queue, not the source of truth |
| Disaster | `down -v` deletes the database and the queue |
| Upgrade | [UPGRADE.md](UPGRADE.md) |

On-call: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
