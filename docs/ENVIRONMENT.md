<!-- section: environment-variables -->
# Environment variables

Secrets and process settings come from the environment. Non-secret behaviour is in `config.yaml` ([CONFIGURATION.md](CONFIGURATION.md)). Start from [`.env.example`](../.env.example).

- Variables are read from the process environment, then from a `.env` file in the working directory. Compose passes `.env` to `migrate`, `api` and `worker` with `env_file`.
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

<!-- section: passed-through-to-subprocesses -->
## Passed through to subprocesses

The worker starts `git` and `codex` with an allowlisted environment, not its own. Nothing else (database URL, GitHub, Jira or Slack tokens, other owners' keys) reaches them.

| Subprocess | Variables passed when set |
| --- | --- |
| `git clone` | `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`/`TMP`/`TEMP`, `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (either case), `SSL_CERT_FILE`, `SSL_CERT_DIR`, `GIT_SSL_CAINFO`; plus the run owner's GitHub token through `GIT_ASKPASS`. |
| `codex exec` | `PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`/`TMP`/`TEMP`, `OPENAI_BASE_URL`, `CODEX_HOME` (per-run directory with `shell_environment_policy` and execpolicy rules), `XDG_CACHE_HOME`, `SSL_CERT_FILE`; plus `OPENAI_API_KEY` and `CODEX_API_KEY` set to the run owner's model key for Codex itself. Agent-spawned commands do not inherit those keys (`inherit = "none"` plus a minimal `set`). |

<!-- section: tests-only -->
## Tests only

| Variable | Description |
| --- | --- |
| `TEST_DATABASE_URL` | Postgres URL for database tests; they are skipped without it. See [CONTRIBUTING.md](../CONTRIBUTING.md). |
