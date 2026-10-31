<!-- section: environment-variables -->
# Environment variables

Secrets and process settings come from the environment. Non-secret behaviour is in `config.yaml` ([CONFIGURATION.md](CONFIGURATION.md)). Start from [`.env.example`](../.env.example).

- Variables are read from the process environment, then from a `.env` file in the working directory. Compose passes `.env` to `migrate`, `api` and `worker` with `env_file`. The opt-in **launcher** does **not** use `env_file` — only `LAUNCHER_TOKEN` and explicit non-secret launcher settings (host `.env` is still used for `${LAUNCHER_TOKEN:-}` interpolation).
- Names are case-insensitive for the settings below; the `OWNER_<ID>_*` names must be upper case.
- Values are read once at startup. Recreate the containers after a change (`docker compose up -d --force-recreate api worker`); `restart` does not reload `env_file`.
- Never commit `.env`. Secret values are never printed in startup errors; the error names the variable.

Column **Read by**: `api` (FastAPI), `worker` (ARQ worker and Codex runs), `migrate` (Alembic). "all" means all three.

<!-- section: database-queue-and-processes -->
## Database, queue and processes

| Variable | Default | Read by | Description |
| --- | --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://review:review@localhost:5432/review` | all | Postgres URL (asyncpg driver). Compose builds it from `POSTGRES_PASSWORD` and overrides any value in `.env`. |
| `POSTGRES_PASSWORD` | — (required by Compose) | Compose | Password of the bundled Postgres; substituted into `DATABASE_URL`. Use URL-safe characters (for example `openssl rand -hex 32`). |
| `REDIS_URL` | `redis://localhost:6379/0` | api, worker | ARQ queue. Compose sets `redis://redis:6379/0`. |
| `CONFIG_PATH` | `config.yaml` | all | Path of the YAML config. |
| `WORKER_MAX_JOBS` | `4` | worker | Concurrent review jobs per worker process. |
| `WORKER_JOB_TIMEOUT` | `900` | worker | Seconds before ARQ cancels a job; the run is closed as `REVIEW_TIMEOUT`. Also the base of the progress-status cleanup (timeout + 5 min). |
| `METRICS_PORT` | `9100` | worker | Port of the worker's Prometheus `/metrics`. |
| `LOG_LEVEL` | `INFO` | api, worker | Python log level. |
| `LOG_FORMAT` | `text` | api, worker | `text` or `json`. |
| `OPEN_PR_REVIEW_SERVICE` | `unknown` | api, worker | `service` field in logs. Compose sets `api` / `worker`. |
| `OPEN_PR_REVIEW_DATA_DIR` | unset | api, worker | Extra directory searched first for bundled data files (`prompts/`, `schemas/`). Default search: `/app`, the working directory, the source checkout. Needed only for unusual installs. |
| `RUN_MIGRATIONS` | `false` | — | **Currently not used by any code.** Migrations run in the Compose `migrate` service (`alembic upgrade head`) or by hand. |
| `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES` | unset | migrate | `1`/`true`/`yes` lets the `007_findings_owner_id` downgrade delete duplicate findings. See [UPGRADE.md](UPGRADE.md). |

<!-- section: api-access -->
## API access

| Variable | Default | Read by | Description |
| --- | --- | --- | --- |
| `REVIEW_API_KEY` | `""` | api | Operator key for `/api/v1/reviews*` and `/api/v1/ops/status`, valid for every owner. Generate a long random value; it is not a GitHub token. Without it those endpoints answer `401`. |

<!-- section: github-legacy-default-owner -->
## GitHub (legacy / `default` owner)

Used when there is no `owners:` block, or in mode B. Named owners use the `OWNER_<ID>_*` variables below.

| Variable | Default | Read by | Description |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | `""` | api, worker | Fine-grained PAT (`github_pat_…`). When set it is used instead of App credentials. |
| `GITHUB_PAT` | — | api, worker | Alias of `GITHUB_TOKEN`. |
| `GITHUB_APP_ID` | `0` | api, worker | GitHub App id. |
| `GITHUB_APP_PRIVATE_KEY` | `""` | api, worker | App private key: the PEM (literal `\n` is accepted) or a path to a PEM file. |
| `GITHUB_INSTALLATION_ID` | `0` | api, worker | App installation; `0` takes it from the webhook or looks it up. |
| `GITHUB_WEBHOOK_SECRET` | `""` | api | HMAC secret of `POST /api/v1/pull-request`. Empty, a placeholder (`change-me`, `changeme`, `change_me`) or shorter than 16 characters disables the webhook endpoint (`503`); the rest of the API works. |

Setting `GITHUB_TOKEN`, or both `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY`, counts as "legacy GitHub credentials" and selects mode A or B ([MULTI_OWNER.md](MULTI_OWNER.md#modes)).

<!-- section: model-jira-and-slack-legacy-default-owner -->
## Model, Jira and Slack (legacy / `default` owner)

| Variable | Default | Read by | Description |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | `""` | api (startup validation), worker | Model key. Shared default for every owner without its own key. Inside the Codex sandbox it is also exported as `CODEX_API_KEY` (Codex ignores `OPENAI_API_KEY` alone). |
| `JIRA_BASE_URL` | `""` | worker | Overrides `jira.base_url`. |
| `JIRA_EMAIL` | `""` | worker | Overrides `jira.email`. |
| `JIRA_API_TOKEN` | `""` | worker | Jira API token. |
| `SLACK_BOT_TOKEN` | `""` | worker | Slack bot token, used when `slack.enabled: true`. |

<!-- section: per-owner-variables-owner-id -->
## Per-owner variables (`OWNER_<ID>_*`)

For each named owner, `<ID>` is the owner id upper-cased with `-` replaced by `_` (owner `org-b` → `OWNER_ORG_B_…`). A `*_env` key in the owner block replaces the default name with any variable you choose (for example to share one Jira token between owners).

| Default name | YAML key that renames it | Required | Read by | Description |
| --- | --- | --- | --- | --- |
| `OWNER_<ID>_GITHUB_TOKEN` | `github.token_env` | yes, with `auth: pat` | api, worker | PAT. |
| `OWNER_<ID>_GITHUB_APP_ID` | `github.app_id_env` (or literal `github.app_id`) | yes, with `auth: app` | api, worker | App id. |
| `OWNER_<ID>_GITHUB_APP_PRIVATE_KEY` | `github.private_key_env` (or `github.private_key_file`) | yes, with `auth: app` | api, worker | PEM or path to it. |
| `OWNER_<ID>_GITHUB_WEBHOOK_SECRET` | `github.webhook_secret_env` | no | api | Webhook secret (16+ characters). Missing: webhooks off for this owner. |
| `OWNER_<ID>_REVIEW_API_KEY` | `api.key_env` | no | api | API key scoped to this owner. |
| `OWNER_<ID>_JIRA_API_TOKEN` | `jira.api_token_env` | no | worker | Jira token. Missing with a `jira` block: startup warning, Jira off for this owner. |
| `OWNER_<ID>_SLACK_BOT_TOKEN` | `slack.bot_token_env` | no | worker | Slack token. Missing with an enabled `slack` block: startup warning, Slack off for this owner. |
| `OWNER_<ID>_OPENAI_API_KEY` | `codex.api_key_env` (then required) | no | api (startup validation), worker | Model key; falls back to `OPENAI_API_KEY`. A variable named in `codex.api_key_env` must also be set for the API, or the API does not start. |

A missing variable marked required, or a missing variable named explicitly in `codex.api_key_env`, stops the API and the worker at startup with an error naming it. Other missing variables only log a startup warning.

<!-- section: review-launcher -->
## Review launcher (container backend)

Used only when `codex.runner_backend: container`. Enable via `./scripts/stack.ps1 up -Container` / `./scripts/stack.sh up --container` (or the Compose overlay `docker-compose.container-runner.yml`; see [DEPLOY.md](DEPLOY.md)); the base `docker compose up` / CI build does **not** load the launcher. The **launcher** is the only Compose service that mounts the Docker socket — treat `LAUNCHER_TOKEN` and host socket access as high trust ([SECURITY.md](../SECURITY.md)). An empty `LAUNCHER_TOKEN` does not break compose interpolation; the launcher process and the **worker** refuse to start (fail closed) when the container backend is selected without a token (≥ 16 characters). The **api** service never calls the launcher and does not require `LAUNCHER_TOKEN`. For `container`, `codex.timeout_seconds` plus launcher overhead must stay below `WORKER_JOB_TIMEOUT` / `worker_job_timeout`, Checkout/job dirs under `OPR_CHECKOUT_BASE` are protected by sidecar `.lease` heartbeats (`<dirname>.lease` beside the dir, not inside the worktree) (`OPR_LEASE_HEARTBEAT_SECONDS`); the reaper removes a dir only when its lease is stale (or, if no lease was ever written, after a conservative age bound from `OPR_CHECKOUT_ORPHAN_TTL_SECONDS`). `OPR_REVIEW_ORPHAN_TTL_SECONDS` must be >= max review timeout + container wait grace (fail closed); containers for lifecycle-active jobs are never reaped. Within one ARQ job the worker clamps each Codex call to the remaining `WORKER_JOB_TIMEOUT` budget; for `container` the clamp subtracts the full launcher pipeline overhead so the HTTP read deadline never exceeds the remaining ARQ budget (retry / public alignment / PR description are skipped when too little time remains). Description-budget skips are decided **before** the sticky comment is published so the note appears there. `OPR_CLONE_TIMEOUT_SECONDS` is enforced in `git_clone` (process-group kill on timeout) and further clamped to the remaining ARQ `JobDeadline` (minus a reserved minimal Codex call + overhead); when remaining cannot cover clone + call, PR description is skipped with the budget notice. For `container`, worker startup also requires `codex.timeout_seconds` <= `OPR_MAX_REVIEW_TIMEOUT_SECONDS`. On worker cancel, the executor calls launcher `DELETE /v1/jobs/{job_id}` which marks cancelled, **immediately** kills visible containers, then waits briefly within `LAUNCHER_CANCEL_DEADLINE_SECONDS` (`LAUNCHER_CANCEL_WAIT_SECONDS` capped by remaining budget; deadline < worker DELETE read timeout) for an in-flight launch, removes again, and returns `confirmed: true` only when idle; staging dirs are deleted only when confirmed. Description-budget skips decided after sticky publish refresh the sticky via the same upsert path.

| Variable | Default | Read by | Purpose |
| --- | --- | --- | --- |
| `LAUNCHER_TOKEN` | (required when overlay/container backend is used) | worker, launcher | Shared bearer token for `POST /v1/reviews`. Generate a long random value (`openssl rand -hex 32`). |
| `LAUNCHER_URL` | `http://launcher:8080` | worker | Base URL of the launcher service on the internal network. |
| `OPR_CHECKOUT_BASE` | `/var/opr/checkouts` | worker, launcher | Shared volume root for staged checkouts; launcher rejects path traversal outside this base. |
| `OPR_CHECKOUT_VOLUME` | `opr_checkouts` | launcher | Named Docker volume for review `Type=volume` mounts with `VolumeOptions.Subpath` (not launcher-local binds; requires Engine ≥ 26 / API ≥ 1.45). |
| `OPR_REVIEW_IMAGE` | `opr-review:local` | launcher | Review image tag built from `Dockerfile.review`. |
| `OPR_REVIEW_NETWORK` | `autoreview_review_egress` | launcher | Docker network attached to review containers (not postgres/redis/api). |
| `LAUNCHER_APPARMOR_PROFILE` | `unconfined` | launcher | AppArmor profile for review containers (`apparmor=<value>`). Default `unconfined` (same posture as the worker) so bubblewrap mounts work on AppArmor Linux hosts; set a dedicated profile name when one is installed. |
| `OPR_REVIEW_ORPHAN_TTL_SECONDS` | `7200` | launcher | Reaper removes labeled review containers older than this when the job is **not** lifecycle-active. Must be >= max review timeout + container wait grace (fail closed at launcher startup). |
| `OPR_MAX_REVIEW_TIMEOUT_SECONDS` | `600` | worker, launcher | Max `timeout_seconds` accepted from staged `job.json` (matches default `codex.timeout_seconds`). Keep >= `codex.timeout_seconds`. |
| `OPR_CLONE_TIMEOUT_SECONDS` | `300` | worker, launcher | Hard cap for `git_clone` (kills the git process group on timeout); further clamped to remaining ARQ JobDeadline minus reserved min Codex call + overhead. |
| `OPR_CHECKOUT_ORPHAN_TTL_SECONDS` | `900` (no-lease fallback) | worker, launcher | Conservative age bound for checkout/job dirs that never got a sidecar `<dirname>.lease` (crash before first heartbeat). Live dirs are protected by sidecar lease heartbeats (`OPR_LEASE_HEARTBEAT_SECONDS`; file beside the dir, not inside the worktree; stale after heartbeat×3). |
| `OPR_LEASE_HEARTBEAT_SECONDS` | `30` | worker, launcher | How often a live checkout/job dir refreshes its sidecar `<dirname>.lease` (outside the worktree). Reaper treats a lease as stale after heartbeat×3. |
| `OPR_REVIEW_REAPER_INTERVAL_SECONDS` | `300` | launcher | How often the reaper scans for orphan review containers and stale checkout dirs. |
| `LAUNCHER_CANCEL_DEADLINE_SECONDS` | `4` | launcher | Hard wait bound for DELETE `/v1/jobs/{id}`: cancel work runs in a background worker; the handler waits at most this long then returns `confirmed=false` while Docker cleanup may continue (tombstone already set; duplicate DELETE coalesces; reaper reclaims leftovers). Per Docker call also uses the actual remaining budget (never inflated; under 50ms → skip). Must be ≤ worker DELETE HTTP read timeout (5s) **minus a mandatory 0.5s margin** (max 4.5s; default 4s passes) and above `LAUNCHER_CANCEL_WAIT_SECONDS`. Docker HTTP uses a long-lived async loop + asyncio.timeout (not per-call asyncio.run). |
| `LAUNCHER_CANCEL_WAIT_SECONDS` | `3` | launcher | After kill-first cancel, seconds to wait for an in-flight launch before confirming idle (capped by remaining `LAUNCHER_CANCEL_DEADLINE_SECONDS`). |
| `LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS` | `2` | launcher | Short HTTP timeout per background proxy token revoke attempt (cancel + reaper; ARB34-002/008/009). Does not extend the cancel deadline. Transient failures retry with backoff until the token TTL window elapses. |
| `LAUNCHER_CANCEL_REVOKE_MAX_PENDING` | `32` | launcher | Max in-flight background proxy revokes on the dedicated revoke loop (ARB34-005/009). When saturated, `schedule_proxy_token_revoke` returns false: cancel notes an in-memory retry; reaper **keeps** the container/checkout and retries next pass. Same `job_id` is deduped. No persistent tombstones — launcher restart worst case is bounded by `LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS` (default = max review timeout + grace). `_CANCEL_POOL` stays exclusive to Docker cancel. |
| `LAUNCHER_REVOKE_RETRY_INITIAL_BACKOFF_SECONDS` | `0.5` | launcher | Initial backoff between transient revoke retries (ARB34-009). |
| `LAUNCHER_REVOKE_RETRY_MAX_BACKOFF_SECONDS` | `30` | launcher | Cap for exponential backoff between transient revoke retries (ARB34-009). |
| `LAUNCHER_REVOKE_RETRY_SET_MAX` | `256` | launcher | Max in-memory job ids waiting for a later revoke schedule after queue reject (ARB34-009; size + TTL bounded; lost on restart). |
| `LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS` | `660` (`OPR_MAX_REVIEW_TIMEOUT_SECONDS` + 60) | launcher | Wall-clock bound for transient revoke retries from first attempt (ARB34-009). Default matches model-proxy max token TTL; no persistent tombstones — launcher restart worst case is this window. |
| `OPR_DEFAULT_MODEL` | (from staged `job.json`) | launcher | Fallback model name if staged `job.json` omits `model`. |
| `OPR_REVIEW_SECCOMP_PROFILE` | `/opt/opr-seccomp/review-bwrap.json` | launcher | Path to the baked review seccomp JSON (override for tests/dev only). |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | launcher | Docker Engine endpoint. Compose sets the unix socket; do not point this at a remote daemon from an untrusted network. |
| `PROXY_ISSUER_TOKEN` | (required for container backend) | worker, launcher, model-proxy | Shared issuer bearer; launcher reaper/cancel revoke leftover job tokens. |
| `MODEL_PROXY_URL` | `http://model-proxy:8081` | worker, launcher | Issuer base URL (worker issues; launcher revokes on cancel/reaper). |
| `OPR_MODEL_PROXY_BASE_URL` | `http://model-proxy:8081/v1` | launcher | Written into review `CODEX_HOME/config.toml` as the custom provider `base_url`. |

<!-- section: model-proxy-m3 -->
## Model proxy (M3)

Used when the container-runner overlay includes `model-proxy` (opt-in Compose). The proxy holds real model keys; the worker issues short-lived `opr_job_` tokens and the launcher injects only that token as `CODEX_API_KEY` plus a generated `config.toml` (`model_provider = "opr_proxy"`). Review containers attach only to the internal `review_egress` network (no internet); `model-proxy` reaches the upstream API via `proxy_egress`. `in_process` does **not** use the proxy in M3. Codex 0.164.0: no separate reliable knob was found to disable the built-in chatgpt.com model-catalog fetch beyond using a custom provider (which avoided that path in the spike); network isolation is the backstop.

The proxy sends upstream `Accept-Encoding: identity` and strips representation headers `content-encoding` / `content-length` / `transfer-encoding` from the client response (so a decoded body is never forwarded with a stale encoding).


| Variable | Default | Read by | Notes |
| --- | --- | --- | --- |
| `PROXY_ISSUER_TOKEN` | (required to start model-proxy / container backend) | model-proxy, worker, launcher | Shared bearer for `POST/DELETE /internal/jobs/{job_id}/token`. Generate with `openssl rand -hex 32` (>= 16 chars). |
| `MODEL_PROXY_URL` | `http://model-proxy:8081` | worker, launcher | Base URL for issuer/revoke calls. |
| `OPR_MODEL_PROXY_BASE_URL` | `http://model-proxy:8081/v1` | launcher | `base_url` in the review container `config.toml` custom provider. |
| `OPENAI_API_KEY` | `""` | model-proxy (via `model-proxy.env`; also api/worker as today) | Default upstream model key inside the proxy. For the container-runner overlay, set it only in `model-proxy.env` (not Compose `environment:`, which would override/empty `env_file`). |
| `PROXY_OWNER_<ID>_OPENAI_API_KEY` | `""` | model-proxy | Legacy per-owner upstream key (`<ID>` = `[A-Z0-9_]+`). Used only as a fallback for owner ids that do **not** contain `-`: lookup uses `PROXY_OWNER_<OWNER_ID.upper()>_OPENAI_API_KEY`. Hyphenated owner ids (`org-a`) must use `PROXY_OWNER_KEY_ENVS` (no hyphen→underscore remap). Falls back to `OPENAI_API_KEY` when no owner key matches. |
| `PROXY_OWNER_KEY_ENVS` | `{}` | model-proxy | JSON object mapping exact `owner_id` → **name** of the env var that holds that owner's upstream key (secrets stay in env). Required for hyphenated ids (`org-a`). Startup fails if two owners map to the same env var, a referenced env is missing/empty, or case-variant owners share one env. Example: `{"org-a":"ORG_A_HYPHEN_KEY","org_a":"ORG_A_UNDER_KEY"}`. Set only in `model-proxy.env` for the container-runner overlay (not Compose `environment:`). |
| `PROXY_PROVIDER` | `openai` | model-proxy | Upstream provider id (only `openai` in M3 PR2; extensible later). |
| `PROXY_UPSTREAM_HOST` | `api.openai.com` | model-proxy | Fixed upstream host (callers cannot override). |
| `PROXY_UPSTREAM_SCHEME` | `https` | model-proxy | `https` or `http` (tests). |
| `OPR_MAX_REVIEW_TIMEOUT_SECONDS` | `600` | model-proxy, worker, launcher | Caps job-token TTL with grace. |
| `PROXY_TOKEN_GRACE_SECONDS` | `60` | model-proxy | Added to `timeout_seconds` for token TTL (capped by max review + grace). |
| `PROXY_MAX_BODY_BYTES` | `4194304` (4 MiB) | model-proxy | Max request body size: oversized `Content-Length` rejected with 413 before read; chunked/unknown length read incrementally and aborted at the cap. |
| `PROXY_MAX_REQUESTS_PER_JOB` | `50` | model-proxy | Max proxied requests per job token. |
| `PROXY_MAX_BYTES_PER_JOB` | `16777216` (16 MiB) | model-proxy | Max in+out bytes per job token; outbound counted incrementally/atomically while consuming upstream (stream and non-stream) so concurrent requests cannot jointly exceed the cap. |
| `PROXY_MAX_CONCURRENT_PER_JOB` | `2` | model-proxy | Max concurrent in-flight proxied requests per job (slot reserved before reading the client body; released on upload failure/timeout/413). |
| `PROXY_STREAM_IDLE_TIMEOUT_SECONDS` | `300` | model-proxy | Wall-clock idle bound between chunks for both client upload (`request.stream`) and upstream response (`asyncio.timeout` per next-chunk wait; HTTPX float timeout is only a soft per-phase backstop). |
| `PROXY_UPSTREAM_TIMEOUT_SECONDS` | `660` | model-proxy | Wall-clock overall deadline for reading the client request body and for the whole upstream request+response (not HTTPX inactivity alone); also stops at job-token expiry and closes the upstream response. |
| `OPR_PROXY_FINISH_REVOKE_TIMEOUT_SECONDS` | `2` | worker | Wall-clock bound for best-effort `revoke_job_token` in `ContainerReviewExecutor.run` finally (ARB34-004). Lease cleanup and the review result must not wait on a hung proxy; reaper/launcher revoke remain backstops. |


Proxy-only secrets file: copy [`model-proxy.env.example`](../model-proxy.env.example) to `model-proxy.env` (gitignored), restrict permissions (`chmod 600` / Windows ACLs), and put `OPENAI_API_KEY`, `PROXY_OWNER_KEY_ENVS`, and any env vars named by that map there. Compose loads it via `env_file` on **model-proxy only** (never worker/launcher).

Public allowlist on the proxy: **`POST /v1/responses`** only (SSE supported). No WebSocket / CONNECT. Logs never include tokens, keys, or bodies.

<!-- section: passed-through-to-subprocesses -->
## Passed through to subprocesses

The worker starts `git` and `codex` with an allowlisted environment, not its own. Nothing else (database URL, GitHub, Jira or Slack tokens, other owners' keys) reaches them.

| Subprocess | Variables passed when set |
| --- | --- |
| `git clone` | `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`/`TMP`/`TEMP`, `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (either case), `SSL_CERT_FILE`, `SSL_CERT_DIR`, `GIT_SSL_CAINFO`; plus the run owner's GitHub token through `GIT_ASKPASS`. |
| `codex exec` | `PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`/`TMP`/`TEMP`, `OPENAI_BASE_URL`, `CODEX_HOME` (per-run directory with `shell_environment_policy` and execpolicy rules), `XDG_CACHE_HOME`, `SSL_CERT_FILE`; plus `OPENAI_API_KEY` and `CODEX_API_KEY` set to the run owner's model key for Codex itself. Agent-spawned commands do not inherit those keys (`inherit = "core"` plus default `KEY`/`SECRET`/`TOKEN` excludes; PATH is not overridden in policy). Execpolicy is a best-effort denylist (no allow rules; no Codex default-deny). AR-001/AR-002 accepted as platform limits; sandbox fails closed unless `codex.allow_unsandboxed`; when that flag is false only `read-only` is accepted for `codex.sandbox` - see [SECURITY.md](../SECURITY.md). |

<!-- section: tests-only -->
## Tests only

| Variable | Description |
| --- | --- |
| `TEST_DATABASE_URL` | Postgres URL for database tests; they are skipped without it. See [CONTRIBUTING.md](../CONTRIBUTING.md). |
