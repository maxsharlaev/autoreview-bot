<!-- section: environment-variables -->
# Переменные окружения

Секреты и настройки процессов берутся из окружения. Несекретное поведение — в `config.yaml` ([CONFIGURATION.md](CONFIGURATION.md)). Начните с [`.env.example`](../../.env.example).

- Переменные читаются из окружения процесса, затем из файла `.env` в рабочем каталоге. Compose передаёт `.env` в `migrate`, `api` и `worker` через `env_file`. Opt-in **launcher** **не** использует `env_file` — только `LAUNCHER_TOKEN` и явные несекретные настройки (host `.env` по-прежнему нужен для интерполяции `${LAUNCHER_TOKEN:-}`).
- Для настроек ниже имена нечувствительны к регистру; имена `OWNER_<ID>_*` должны быть в верхнем регистре.
- Значения читаются один раз при старте. После изменения пересоздайте контейнеры (`docker compose up -d --force-recreate api worker`); `restart` не перечитывает `env_file`.
- Никогда не коммитьте `.env`. Значения секретов не печатаются в ошибках старта; ошибка называет переменную.

Колонка **Читает**: `api` (FastAPI), `worker` (ARQ worker и запуски Codex), `migrate` (Alembic). «все» — все три.

<!-- section: database-queue-and-processes -->
## База данных, очередь и процессы

| Переменная | По умолчанию | Читает | Описание |
| --- | --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://review:review@localhost:5432/review` | все | URL Postgres (драйвер asyncpg). Compose собирает его из `POSTGRES_PASSWORD` и переопределяет значение из `.env`. |
| `POSTGRES_PASSWORD` | — (обязателен для Compose) | Compose | Пароль встроенного Postgres; подставляется в `DATABASE_URL`. Используйте URL-безопасные символы (например `openssl rand -hex 32`). |
| `REDIS_URL` | `redis://localhost:6379/0` | api, worker | Очередь ARQ. Compose задаёт `redis://redis:6379/0`. |
| `CONFIG_PATH` | `config.yaml` | все | Путь к YAML-конфигу. |
| `WORKER_MAX_JOBS` | `4` | worker | Параллельных job ревью на процесс worker. |
| `WORKER_JOB_TIMEOUT` | `900` | worker | Секунд до отмены job в ARQ; прогон закрывается как `REVIEW_TIMEOUT`. Также основа очистки статусов прогресса (таймаут + 5 мин). |
| `METRICS_PORT` | `9100` | worker | Порт Prometheus `/metrics` worker'а. |
| `LOG_LEVEL` | `INFO` | api, worker | Уровень логирования Python. |
| `LOG_FORMAT` | `text` | api, worker | `text` или `json`. |
| `OPEN_PR_REVIEW_SERVICE` | `unknown` | api, worker | Поле `service` в логах. Compose задаёт `api` / `worker`. |
| `OPEN_PR_REVIEW_DATA_DIR` | не задана | api, worker | Дополнительный каталог, где в первую очередь ищутся встроенные файлы данных (`prompts/`, `schemas/`). Поиск по умолчанию: `/app`, рабочий каталог, исходники. Нужна только для нестандартных установок. |
| `RUN_MIGRATIONS` | `false` | — | **Сейчас не используется кодом.** Миграции выполняет сервис Compose `migrate` (`alembic upgrade head`) или вы вручную. |
| `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES` | не задана | migrate | `1`/`true`/`yes` разрешает downgrade `007_findings_owner_id` удалять дубликаты замечаний. См. [UPGRADE.md](UPGRADE.md). |

<!-- section: api-access -->
## Доступ к API

| Переменная | По умолчанию | Читает | Описание |
| --- | --- | --- | --- |
| `REVIEW_API_KEY` | `""` | api | Операторский ключ для `/api/v1/reviews*` и `/api/v1/ops/status`, действует для всех owner'ов. Сгенерируйте длинное случайное значение; это не токен GitHub. Без него эти эндпойнты отвечают `401`. |

<!-- section: github-legacy-default-owner -->
## GitHub (legacy / owner `default`)

Используется без блока `owners:` или в режиме B. Именованные owner'ы используют переменные `OWNER_<ID>_*` ниже.

| Переменная | По умолчанию | Читает | Описание |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | `""` | api, worker | Fine-grained PAT (`github_pat_…`). Если задан, используется вместо кредов App. |
| `GITHUB_PAT` | — | api, worker | Алиас `GITHUB_TOKEN`. |
| `GITHUB_APP_ID` | `0` | api, worker | Id GitHub App. |
| `GITHUB_APP_PRIVATE_KEY` | `""` | api, worker | Приватный ключ App: PEM (литеральные `\n` допускаются) или путь к PEM-файлу. |
| `GITHUB_INSTALLATION_ID` | `0` | api, worker | Установка App; `0` — взять из webhook или найти. |
| `GITHUB_WEBHOOK_SECRET` | `""` | api | HMAC-секрет `POST /api/v1/pull-request`. Пустой, заглушка (`change-me`, `changeme`, `change_me`) или короче 16 символов отключает эндпойнт webhook (`503`); остальной API работает. |

Заданный `GITHUB_TOKEN` или пара `GITHUB_APP_ID` и `GITHUB_APP_PRIVATE_KEY` считаются «legacy-кредами GitHub» и выбирают режим A или B ([MULTI_OWNER.md](MULTI_OWNER.md#режимы)).

<!-- section: model-jira-and-slack-legacy-default-owner -->
## Модель, Jira и Slack (legacy / owner `default`)

| Переменная | По умолчанию | Читает | Описание |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | `""` | api (проверка при старте), worker | Ключ модели. Общий ключ по умолчанию для всех owner'ов без своего ключа. Внутри песочницы Codex также экспортируется как `CODEX_API_KEY` (одну `OPENAI_API_KEY` Codex игнорирует). |
| `JIRA_BASE_URL` | `""` | worker | Переопределяет `jira.base_url`. |
| `JIRA_EMAIL` | `""` | worker | Переопределяет `jira.email`. |
| `JIRA_API_TOKEN` | `""` | worker | API-токен Jira. |
| `SLACK_BOT_TOKEN` | `""` | worker | Токен бота Slack, используется при `slack.enabled: true`. |

<!-- section: per-owner-variables-owner-id -->
## Переменные owner'ов (`OWNER_<ID>_*`)

Для каждого именованного owner'а `<ID>` — id owner'а в верхнем регистре, `-` заменён на `_` (owner `org-b` → `OWNER_ORG_B_…`). Ключ `*_env` в блоке owner'а заменяет имя по умолчанию любой выбранной переменной (например, чтобы несколько owner'ов использовали один токен Jira).

| Имя по умолчанию | YAML-ключ, который его переименовывает | Обязательна | Читает | Описание |
| --- | --- | --- | --- | --- |
| `OWNER_<ID>_GITHUB_TOKEN` | `github.token_env` | да, при `auth: pat` | api, worker | PAT. |
| `OWNER_<ID>_GITHUB_APP_ID` | `github.app_id_env` (или литерал `github.app_id`) | да, при `auth: app` | api, worker | Id App. |
| `OWNER_<ID>_GITHUB_APP_PRIVATE_KEY` | `github.private_key_env` (или `github.private_key_file`) | да, при `auth: app` | api, worker | PEM или путь к нему. |
| `OWNER_<ID>_GITHUB_WEBHOOK_SECRET` | `github.webhook_secret_env` | нет | api | Секрет webhook (16+ символов). Нет — webhooks для этого owner'а выключены. |
| `OWNER_<ID>_REVIEW_API_KEY` | `api.key_env` | нет | api | API key этого owner'а. |
| `OWNER_<ID>_JIRA_API_TOKEN` | `jira.api_token_env` | нет | worker | Токен Jira. Нет при блоке `jira` — предупреждение при старте, Jira для этого owner'а выключена. |
| `OWNER_<ID>_SLACK_BOT_TOKEN` | `slack.bot_token_env` | нет | worker | Токен Slack. Нет при включённом блоке `slack` — предупреждение при старте, Slack для этого owner'а выключен. |
| `OWNER_<ID>_OPENAI_API_KEY` | `codex.api_key_env` (тогда обязательна) | нет | api (проверка при старте), worker | Ключ модели; иначе используется `OPENAI_API_KEY`. Переменная, названная в `codex.api_key_env`, должна быть задана и для API, иначе API не стартует. |

Отсутствие обязательной переменной или переменной, явно названной в `codex.api_key_env`, останавливает API и worker при старте с ошибкой, называющей её. Отсутствие остальных переменных только пишет предупреждение при старте.

<!-- section: review-launcher -->
## Review launcher (container backend)

Нужен только при `codex.runner_backend: container`. Включайте через `./scripts/stack.ps1 up -Container` / `./scripts/stack.sh up --container` (или Compose overlay `docker-compose.container-runner.yml`; см. [DEPLOY.md](DEPLOY.md)); обычный `docker compose up` / CI build **не** поднимает launcher. Сервис **launcher** — единственный в Compose, кто монтирует Docker socket; `LAUNCHER_TOKEN` и доступ к socket — зона высокого доверия ([SECURITY.md](../../SECURITY.md)). Пустой `LAUNCHER_TOKEN` не ломает интерполяцию compose; процесс launcher и **worker** отказываются стартовать (fail closed), если выбран container backend без токена (≥ 16 символов). Сервис **api** не вызывает launcher и не требует `LAUNCHER_TOKEN`. Для `container` сумма `codex.timeout_seconds` и launcher overhead должна быть меньше `WORKER_JOB_TIMEOUT` / `worker_job_timeout`, Checkout/job dirs под `OPR_CHECKOUT_BASE` защищены sidecar `.lease` heartbeats (`<dirname>.lease` рядом с каталогом, не внутри worktree) (`OPR_LEASE_HEARTBEAT_SECONDS`); reaper удаляет dir только при stale lease (или без lease — консервативный age bound из `OPR_CHECKOUT_ORPHAN_TTL_SECONDS`). `OPR_REVIEW_ORPHAN_TTL_SECONDS` должен быть >= max review timeout + container wait grace (fail closed); контейнеры lifecycle-active jobs не reap'ятся. В одном ARQ job worker ограничивает каждый Codex-вызов оставшимся бюджетом `WORKER_JOB_TIMEOUT`; для `container` clamp вычитает полный launcher pipeline overhead, чтобы HTTP read deadline не превышал остаток ARQ-бюджета (retry / public alignment / PR description пропускаются, если времени мало). Пропуск PR description по бюджету решается **до** публикации sticky-комментария, чтобы заметка попала в него. `OPR_CLONE_TIMEOUT_SECONDS` жёстко лимитирует `git_clone` (kill process group по таймауту) и дополнительно clamp'ится к оставшемуся ARQ `JobDeadline` (минус резерв на минимальный Codex-вызов + overhead); если бюджета не хватает на clone + call, PR description пропускается с budget notice. Для `container` worker startup также требует `codex.timeout_seconds` <= `OPR_MAX_REVIEW_TIMEOUT_SECONDS`. При cancel worker вызывает launcher `DELETE /v1/jobs/{job_id}`: помечает cancelled, **сразу** убивает видимые контейнеры, затем коротко ждёт в пределах `LAUNCHER_CANCEL_DEADLINE_SECONDS` (`LAUNCHER_CANCEL_WAIT_SECONDS` ограничен остатком бюджета; deadline < worker DELETE read timeout) in-flight launch, удаляет снова; `confirmed: true` только когда idle — staging удаляется только тогда. Заметки о бюджете после sticky-публикации обновляют sticky тем же upsert.

| Variable | Default | Read by | Purpose |
| --- | --- | --- | --- |
| `LAUNCHER_TOKEN` | (required when overlay/container backend is used) | worker, launcher | Общий bearer-токен для `POST /v1/reviews`. Сгенерируйте длинное случайное значение (`openssl rand -hex 32`). |
| `LAUNCHER_URL` | `http://launcher:8080` | worker | Base URL launcher во внутренней сети. |
| `OPR_CHECKOUT_BASE` | `/var/opr/checkouts` | worker, launcher | Корень общего тома со staged checkout; launcher отвергает path traversal вне этой базы. |
| `OPR_CHECKOUT_VOLUME` | `opr_checkouts` | launcher | Имя Docker volume для Mounts `Type=volume` + `VolumeOptions.Subpath` (не launcher-local bind; нужен Engine ≥ 26 / API ≥ 1.45). |
| `OPR_REVIEW_IMAGE` | `opr-review:local` | launcher | Тег review-образа из `Dockerfile.review`. |
| `OPR_REVIEW_NETWORK` | `autoreview_review_egress` | launcher | Docker-сеть review-контейнеров (не postgres/redis/api). |
| `LAUNCHER_APPARMOR_PROFILE` | `unconfined` | launcher | AppArmor-профиль review-контейнеров (`apparmor=<value>`). Default `unconfined` (как у worker), чтобы bubblewrap mount работал на Linux с AppArmor; задайте имя dedicated profile, когда он установлен. |
| `OPR_REVIEW_ORPHAN_TTL_SECONDS` | `7200` | launcher | Reaper удаляет labeled review-контейнеры старше TTL, если job **не** lifecycle-active. Должен быть >= max review timeout + container wait grace (fail closed при старте launcher). |
| `OPR_MAX_REVIEW_TIMEOUT_SECONDS` | `600` | worker, launcher | Макс. `timeout_seconds` из staged `job.json` (как default `codex.timeout_seconds`). Держите >= `codex.timeout_seconds`. |
| `OPR_CLONE_TIMEOUT_SECONDS` | `300` | worker, launcher | Hard cap for `git_clone` (kills the git process group on timeout); further clamped to remaining ARQ JobDeadline minus reserved min Codex call + overhead. |
| `OPR_CHECKOUT_ORPHAN_TTL_SECONDS` | `900` (no-lease fallback) | worker, launcher | Консервативный age bound для checkout/job dirs без sidecar `<dirname>.lease` (crash до первого heartbeat). Живые dirs защищены sidecar lease heartbeats (`OPR_LEASE_HEARTBEAT_SECONDS`; файл рядом с каталогом, не внутри worktree; stale после heartbeat×3). |
| `OPR_LEASE_HEARTBEAT_SECONDS` | `30` | worker, launcher | Как часто live checkout/job-каталог обновляет sidecar `<dirname>.lease` (вне worktree). Reaper считает lease stale после heartbeat×3. |
| `OPR_REVIEW_REAPER_INTERVAL_SECONDS` | `300` | launcher | Как часто reaper сканирует orphan review-контейнеры. |
| `LAUNCHER_CANCEL_DEADLINE_SECONDS` | `4` | launcher | Жёсткий предел ожидания DELETE `/v1/jobs/{id}`: cancel выполняется в background-worker; handler ждёт не дольше этого времени и возвращает `confirmed=false`, пока Docker-очистка может продолжаться (tombstone уже выставлен; повторный DELETE coalesces; leftovers забирает reaper). Каждый вызов Docker — только фактический остаток бюджета (не завышается; меньше 50 мс → skip). Должен быть ≤ worker DELETE HTTP read timeout (5s) **минус обязательный запас 0.5 с** (макс. 4.5 с; default 4 с проходит) и больше `LAUNCHER_CANCEL_WAIT_SECONDS`. Docker HTTP — long-lived async loop + asyncio.timeout (не per-call asyncio.run). |
| `LAUNCHER_CANCEL_WAIT_SECONDS` | `3` | launcher | После kill-first cancel — секунды ожидания in-flight launch перед confirmed idle (ограничено остатком `LAUNCHER_CANCEL_DEADLINE_SECONDS`). |
| `OPR_DEFAULT_MODEL` | (from staged `job.json`) | launcher | Fallback-имя модели, если в staged `job.json` нет `model`. |
| `OPR_REVIEW_SECCOMP_PROFILE` | `/opt/opr-seccomp/review-bwrap.json` | launcher | Путь к seccomp JSON review-контейнера (override только для tests/dev). |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | launcher | Endpoint Docker Engine. Compose задаёт unix socket; не направляйте на удалённый daemon из ненадёжной сети. |
| `X-Model-Api-Key` (HTTP header) | — | launcher ← worker | Только M2: ключ модели в env процесса Codex. Уйдёт после model-API proxy в M3. |

<!-- section: model-proxy-m3 -->
## Model proxy (M3)

Нужен, когда overlay container-runner поднимает `model-proxy` (opt-in Compose). Proxy хранит настоящие ключи модели; контейнеры ревью будут использовать короткоживущие job-токены (подключение — в следующем PR). **Issuer** — worker (`PROXY_ISSUER_TOKEN`). Режим `in_process` в M3 **не** использует proxy.

| Переменная | По умолчанию | Кто читает | Заметки |
| --- | --- | --- | --- |
| `PROXY_ISSUER_TOKEN` | (нужен для старта model-proxy) | model-proxy, worker | Общий bearer для `POST/DELETE /internal/jobs/{job_id}/token`. Сгенерируйте `openssl rand -hex 32` (>= 16 символов). |
| `MODEL_PROXY_URL` | `http://model-proxy:8081` | worker | Базовый URL для issuer-вызовов (и позже для wiring ревью). |
| `OPENAI_API_KEY` | `""` | model-proxy (и api/worker как сейчас) | Upstream-ключ модели по умолчанию внутри proxy. |
| `PROXY_OWNER_<ID>_OPENAI_API_KEY` | `""` | model-proxy | Upstream-ключ на владельца; иначе `OPENAI_API_KEY`. |
| `PROXY_PROVIDER` | `openai` | model-proxy | Id провайдера (в M3 PR2 только `openai`; позже расширим). |
| `PROXY_UPSTREAM_HOST` | `api.openai.com` | model-proxy | Фиксированный upstream host (клиент не переопределяет). |
| `PROXY_UPSTREAM_SCHEME` | `https` | model-proxy | `https` или `http` (тесты). |
| `OPR_MAX_REVIEW_TIMEOUT_SECONDS` | `600` | model-proxy, worker, launcher | Верхняя граница TTL job-токена вместе с grace. |
| `PROXY_TOKEN_GRACE_SECONDS` | `60` | model-proxy | Добавка к `timeout_seconds` для TTL токена. |
| `PROXY_MAX_BODY_BYTES` | `4194304` (4 MiB) | model-proxy | Макс. размер тела запроса. |
| `PROXY_MAX_REQUESTS_PER_JOB` | `50` | model-proxy | Макс. число proxied-запросов на job-токен. |
| `PROXY_MAX_BYTES_PER_JOB` | `16777216` (16 MiB) | model-proxy | Макс. байт in+out на job-токен. |
| `PROXY_MAX_CONCURRENT_PER_JOB` | `2` | model-proxy | Макс. одновременных upstream-потоков на job. |
| `PROXY_STREAM_IDLE_TIMEOUT_SECONDS` | `300` | model-proxy | Idle-лимит для SSE (read timeout). |
| `PROXY_UPSTREAM_TIMEOUT_SECONDS` | `660` | model-proxy | Общий HTTP-таймаут upstream. |

Публичный allowlist proxy: только **`POST /v1/responses`** (SSE поддерживается). Без WebSocket / CONNECT. Логи не содержат токены, ключи и тела запросов.

<!-- section: passed-through-to-subprocesses -->
## Передаётся подпроцессам

Worker запускает `git` и `codex` с окружением из allowlist, а не со своим. Больше ничего (URL базы, токены GitHub, Jira или Slack, ключи других owner'ов) до них не доходит.

| Подпроцесс | Переменные, передаваемые если заданы |
| --- | --- |
| `git clone` | `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`/`TMP`/`TEMP`, `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (в любом регистре), `SSL_CERT_FILE`, `SSL_CERT_DIR`, `GIT_SSL_CAINFO`; плюс токен GitHub owner'а прогона через `GIT_ASKPASS`. |
| `codex exec` | `PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`/`TMP`/`TEMP`, `OPENAI_BASE_URL`, `CODEX_HOME` (каталог на каждый запуск с `shell_environment_policy` и правилами execpolicy), `XDG_CACHE_HOME`, `SSL_CERT_FILE`; плюс `OPENAI_API_KEY` и `CODEX_API_KEY` со значением ключа модели owner'а именно для процесса Codex. Команды, которые запускает агент, эти ключи не наследуют (`inherit = "core"` плюс исключения `KEY`/`SECRET`/`TOKEN` по умолчанию; PATH в policy не переопределяется). Execpolicy — best-effort denylist (no allow rules; no Codex default-deny). AR-001/AR-002 приняты как ограничения платформы; sandbox fail-closed, пока не включён `codex.allow_unsandboxed`; при false для `codex.sandbox` допускается только `read-only` — см. [SECURITY.md](../../SECURITY.md). |

<!-- section: tests-only -->
## Только для тестов

| Переменная | Описание |
| --- | --- |
| `TEST_DATABASE_URL` | URL Postgres для тестов с базой; без него они пропускаются. См. [CONTRIBUTING.md](../../CONTRIBUTING.md). |
