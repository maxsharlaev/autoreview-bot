<!-- section: environment-variables -->
# Переменные окружения

Секреты и настройки процессов берутся из окружения. Несекретное поведение — в `config.yaml` ([CONFIGURATION.md](CONFIGURATION.md)). Начните с [`.env.example`](../../.env.example).

- Переменные читаются из окружения процесса, затем из файла `.env` в рабочем каталоге. Compose передаёт `.env` в `migrate`, `api` и `worker` через `env_file`.
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
