<!-- section: github-app-pat-and-trigger-setup -->
# Настройка GitHub App, PAT и триггера

Полный деплой хоста (Compose, сеть, Prometheus): [DEPLOY.md](DEPLOY.md). Английская версия: [GITHUB_SETUP.md](../GITHUB_SETUP.md).

Сервис принимает события Pull request на `POST /api/v1/pull-request`. Этот эндпойнт требует только валидный `X-Hub-Signature-256` (HMAC-SHA256 с `GITHUB_WEBHOOK_SECRET`); API key не нужен, потому что webhooks GitHub не умеют отправлять свои заголовки. Если `GITHUB_WEBHOOK_SECRET` не настроен (пустой, заглушка или короче 16 символов), эндпойнт webhook отключён (`503`), но сервис стартует, и `/api/v1/reviews` продолжает работать с `REVIEW_API_KEY`. Никогда не кладите креды в URL webhook.

`GET /health`, `GET /is-ready` и `GET /metrics` открыты для проб и Prometheus; worker отдаёт `GET http://worker:9100/metrics`. Снимок очереди: `GET /api/v1/ops/status` (API key). Справочник API: [API.md](API.md).

<!-- section: where-secrets-go -->
## Где хранятся секреты

| Значение | Где | Назначение |
| --- | --- | --- |
| `REVIEW_API_KEY` | `.env` сервера | Защищает API ревью и эксплуатации |
| `AI_REVIEW_API_KEY` | **Secrets** организации или репозитория GitHub | То же значение, что `REVIEW_API_KEY` (или ключ owner'а) |
| `GITHUB_WEBHOOK_SECRET` | Webhook GitHub App **или** репозитория **и** `.env` сервера | HMAC `X-Hub-Signature-256` |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | Только `.env` сервера | JWT App → installation token. С PAT не нужны |
| `GITHUB_TOKEN` / `GITHUB_PAT` | Только `.env` сервера | Fine-grained PAT (`github_pat_*`); если задан, используется вместо App |
| `OPENAI_API_KEY`, токен Jira | Только `.env` сервера / секреты хоста | Codex и Jira. В секретах ревьюируемых репозиториев не нужны |

Сгенерируйте длинный случайный ключ сервиса (например `openssl rand -hex 32`); он не связан с PAT GitHub. Все переменные: [ENVIRONMENT.md](ENVIRONMENT.md).

<!-- section: option-a-fine-grained-pat -->
## Вариант A: fine-grained PAT

Без GitHub App достаточно PAT (`github_pat_*`) с доступом к ревьюируемым репозиториям. В `.env`:

```bash
GITHUB_TOKEN=github_pat_...
# or GITHUB_PAT=github_pat_...
REVIEW_API_KEY=<service API key, not the PAT>
GITHUB_WEBHOOK_SECRET=<repository webhook secret, if you use a webhook>
```

Права на репозиторий:

- **Pull requests**: Read and write (sticky-комментарий, заголовок/описание PR).
- **Contents**: Read-only (для `git clone`). Бот никогда не пишет в содержимое репозитория.
- **Commit statuses**: Read and write для статуса прогресса `autoreview` (classic PAT: `repo:status`). Необязательно; без него ревью идёт без статуса.

Оставьте поля App (`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`) пустыми; с PAT JWT App не используется. Комментарии в PR появляются от аккаунта владельца токена. Сначала проверьте на одном репозитории и убедитесь, что работают и clone, и комментарий.

<!-- section: option-b-github-app -->
## Вариант B: GitHub App

1. **Settings** организации (или аккаунта) → **Developer settings** → **GitHub Apps** → **New GitHub App**.
2. Имя, например `Autoreview`. **Homepage URL**: URL сервиса или внутренняя документация.
3. **Webhook**: либо выключите его и запускайте ревью через GitHub Actions (App всё равно даёт токен для чтения PR, clone и комментариев), либо включите с `https://review.example.com/api/v1/pull-request` (без ключа в URL) и одинаковым `GITHUB_WEBHOOK_SECRET` в App и на сервере.
4. **Repository permissions**:
   - **Contents**: Read-only
   - **Pull requests**: Read and write
   - **Metadata**: Read-only
   - **Checks**: Read and write, для check run `autoreview` (необязательно; без него ревью идёт без статуса)
5. С webhook подпишитесь только на события **Pull request**.
6. Where can this App be installed: **Only on this account** (или any account, если вы обслуживаете несколько организаций одним App, см. [MULTI_OWNER.md](MULTI_OWNER.md#подключение-ещё-одной-организации)).
7. Создайте App и запишите **App ID** → `GITHUB_APP_ID`.
8. **Generate a private key** и положите содержимое `.pem` (или путь к файлу) в `GITHUB_APP_PRIVATE_KEY`.
9. **Install** App на репозитории из `github.allowed_repos`. **Installation ID** можно указать в `GITHUB_INSTALLATION_ID`; `0` позволяет сервису найти его самому.

Прямому webhook нужен только `GITHUB_WEBHOOK_SECRET`. `AI_REVIEW_API_KEY` нужен для GitHub Actions (`/api/v1/reviews`) и ручных вызовов.

<!-- section: organization-or-repository-secret-for-actions -->
## Секрет организации или репозитория для Actions

1. **Settings** организации → **Secrets and variables** → **Actions** → **New organization secret** (или секреты репозитория).
2. Имя `AI_REVIEW_API_KEY`, значение: `REVIEW_API_KEY` сервера (или ключ owner'а).
3. Repository access: репозитории, которые вызывают API.
4. Переменная `AI_REVIEW_URL`: базовый HTTPS URL без завершающего слеша.

Скопируйте [пример workflow](../../examples/github/ai-review-trigger.yml) в каждый ревьюируемый репозиторий как `.github/workflows/ai-review.yml`. Он ссылается на `maxsharlaev/autoreview-bot@main`; для форка или недоступного workflow измените `uses:` или скопируйте шаги reusable workflow этого репозитория. Он вызывает `POST /api/v1/reviews` с URL PR, поэтому HMAC webhook не нужен. `ci.yml` этого репозитория только тестирует и собирает сервис. Фильтры: [TROUBLESHOOTING.md](TROUBLESHOOTING.md#github-actions-в-ревьюируемом-репозитории).

<!-- section: service-configuration -->
## Конфигурация сервиса

`.env` для App:

```bash
REVIEW_API_KEY=<same as AI_REVIEW_API_KEY>
GITHUB_WEBHOOK_SECRET=<App webhook secret>
GITHUB_APP_ID=<app id>
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----..."
GITHUB_INSTALLATION_ID=0
WORKER_MAX_JOBS=4
```

`config.yaml`:

```yaml
github:
  allowed_repos:
    - example-org/example-repo
    # - example-org/another-repo
```

События из репозиториев вне непустого списка игнорируются (`202`, `repo_not_allowed`); ручные вызовы API получают `403`.

<!-- section: verify -->
## Проверка

После `docker compose up` и публичного HTTPS (или туннеля):

1. Настройте Action, как описано выше.
2. Откройте тестовый не draft PR. В логе worker'а появляется `review_run`; PR получает sticky-комментарий `<!-- open-pr-review -->`.
3. Статус прогона:

   ```bash
   curl -H "Authorization: Bearer $AI_REVIEW_API_KEY" \
     https://review.example.com/api/v1/reviews/<review_run_id>
   ```

<!-- section: parallel-prs -->
## Параллельные PR

Каждый прогон — отдельная job очереди. ARQ worker берёт до `WORKER_MAX_JOBS` job одновременно (по умолчанию **4**); разные PR выполняются параллельно. Повтор того же `head_sha` переиспользует прогон, новый SHA отменяет незавершённый, а конкурентные доставки по одному PR сериализуются ([API.md](API.md#гарантии-конкурентности)).

Чтобы масштабироваться, увеличьте `WORKER_MAX_JOBS` и/или запустите больше реплик worker'а: `docker compose up -d --scale worker=N`. Метрики worker'а не публикуются на хосте, а поставляемый scrape job находит каждую реплику по имени сервиса ([OBSERVABILITY.md](OBSERVABILITY.md)). Ограничения — квота модели и клонирование, а не FastAPI.

<!-- section: several-owners -->
## Несколько owner'ов

У каждого owner'а в `owners:` свои креды GitHub, секрет webhook и необязательный API key owner'а, заданные именами переменных. Именование, два способа подключить ещё одну организацию (тот же App с другой установкой или свой App/PAT с `/api/v1/pull-request/<owner-id>`) и ответы, специфичные для owner'ов: [MULTI_OWNER.md](MULTI_OWNER.md).

<!-- section: common-errors -->
## Частые ошибки

| Симптом | Что проверить |
| --- | --- |
| `503` на webhook | `GITHUB_WEBHOOK_SECRET` не настроен (пустой, заглушка или слишком короткий) |
| `401` на webhook | `X-Hub-Signature-256` отсутствует или неверен: сравните секрет в App и на сервере |
| `401` на `/api/v1/reviews` | `REVIEW_API_KEY` отсутствует или неверен |
| `repo_not_allowed` | Непустой `github.allowed_repos` должен содержать точное `owner/repo` (или `owner/*`) |
| Webhook `202`, комментария нет | Worker запущен; репозиторий разрешён; PR не draft/fork; у автора есть право write ([TROUBLESHOOTING.md](TROUBLESHOOTING.md)) |
| `403` при clone или комментарии | Clone требует Contents read, а доступ токена к репозиториям должен включать этот репозиторий (для организации fine-grained PAT может требовать одобрения владельца); комментарию нужен Pull requests **write** |
| Нет статуса прогресса | Право Checks (App) или Commit statuses (PAT) ([PROGRESS_STATUS.md](PROGRESS_STATUS.md)) |
