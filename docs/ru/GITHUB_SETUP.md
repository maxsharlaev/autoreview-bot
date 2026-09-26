# Установка GitHub App и секретов

Полный деплой хоста (Compose, сеть, Prometheus): [DEPLOY.md](DEPLOY.md).

Сервис принимает события Pull Request на `POST /api/v1/pull-request`. Эндпойнт **закрыт ключом доступа**. Без ключа ответ `401`.

`GET /health`, `GET /is-ready` и `GET /metrics` остаются открытыми для пробы живости и Prometheus. Worker отдельно отдаёт `GET http://worker:9100/metrics`. Снимок очереди: `GET /api/v1/ops/status` (с API key).

## Что куда кладётся

| Секрет | Где хранится | Зачем |
| --- | --- | --- |
| `AI_REVIEW_API_KEY` | GitHub: Organization или Repository **Secrets** | Ключ доступа к API. Тот же значение — в `.env` сервиса как `REVIEW_API_KEY`. |
| `GITHUB_WEBHOOK_SECRET` | GitHub App **или** webhook репозитория **и** `.env` сервиса | HMAC `X-Hub-Signature-256` |
| GitHub App private key | `.env` сервиса `GITHUB_APP_PRIVATE_KEY` | JWT → installation token. Не нужен, если задан PAT. |
| `GITHUB_TOKEN` / `GITHUB_PAT` | только `.env` сервиса | Fine-grained PAT (`github_pat_*`). Если задан — используется вместо App. |
| `OPENAI_API_KEY`, Jira token | только `.env` / secrets хоста сервиса | Codex и Jira. В GitHub Secrets продуктовых репо не нужны. |

Сгенерируйте длинный случайный ключ (например `openssl rand -hex 32`) и используйте **одно и то же** значение:

- `REVIEW_API_KEY` на сервере
- GitHub secret `AI_REVIEW_API_KEY`
Прямой webhook пока не готов для рекомендуемого публичного деплоя: обработчик требует API-ключ и принимает его без обязательной подписи. GitHub рекомендует не помещать ключи в URL доставки. До реализации обязательной проверки подписи используйте GitHub Actions или ручной API. См. [roadmap](ROADMAP.md).

## Вариант: fine-grained PAT

Если GitHub App нет, достаточно PAT (`github_pat_*`) с доступом к тестовому репозиторию. В `.env`:

```bash
GITHUB_TOKEN=github_pat_...
# либо GITHUB_PAT=github_pat_...
REVIEW_API_KEY=<свой ключ API сервиса, не PAT>
GITHUB_WEBHOOK_SECRET=<secret webhook репозитория, если настраиваете webhook>
```

Поля App (`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`) можно оставить пустыми. Если задан PAT, JWT приложения не используется. Комментарии в PR идут от имени владельца токена.

События: GitHub Action в репозитории с кодом ([examples/github/ai-review-trigger.yml](../../examples/github/ai-review-trigger.yml), фильтры paths/branches — [OPERATIONS.md](OPERATIONS.md)) или ручной `POST /api/v1/reviews`. Прямой webhook станет рекомендуемым способом после изменения авторизации. Репозиторий должен быть в `github.allowed_repos`. CI этого сервиса (`ci.yml`) ревью не вызывает.

## 1. Создать GitHub App

1. GitHub → **Settings** организации → **Developer settings** → **GitHub Apps** → **New GitHub App**.
2. Имя, например `Open PR Review`.
3. **Homepage URL** — URL сервиса или внутренний docs.
4. **Webhook**
   - Для текущего MVP можно отключить webhook и запускать ревью через GitHub Actions. App при этом даёт токен для чтения PR и публикации комментария.
   - Когда обязательная проверка подписи будет реализована, включить webhook с URL `https://review.example.com/api/v1/pull-request` без ключа в URL и задать отдельный `GITHUB_WEBHOOK_SECRET`.
5. **Permissions** (Repository):
   - **Contents**: Read-only
   - **Pull requests**: Read and write
   - **Metadata**: Read-only
6. При будущем включении webhook подписаться только на **Pull request**.
7. Where can this App be installed: **Only on this account**.
8. Create App. Запомните **App ID** → `GITHUB_APP_ID`.
9. **Generate a private key** → скачайте `.pem`. Содержимое (или путь к файлу) → `GITHUB_APP_PRIVATE_KEY`.
10. **Install App** на репозитории из `github.allowed_repos` в `config.yaml`. После установки можно взять **Installation ID** → `GITHUB_INSTALLATION_ID` (если `0`, сервис резолвит сам).

Пока прямой webhook не переведён на обязательную подпись, используйте workflow-триггер или ручной API. Secret `AI_REVIEW_API_KEY` нужен для GitHub Actions и ручных вызовов.

## 2. Organization / repository secret

1. GitHub → Organization **Settings** → **Secrets and variables** → **Actions** → **New organization secret**
   (либо Secrets конкретного репозитория).
2. Name: `AI_REVIEW_API_KEY`
3. Value: тот же ключ, что `REVIEW_API_KEY` на сервере.
4. Repository access: репозитории, где будет вызываться API (и где установлен App).

## 3. Конфиг сервиса

`.env`:

```bash
REVIEW_API_KEY=<тот же ключ, что AI_REVIEW_API_KEY>
GITHUB_WEBHOOK_SECRET=<webhook secret из App>
GITHUB_APP_ID=<app id>
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----..."
GITHUB_INSTALLATION_ID=0
WORKER_MAX_JOBS=4
```

`config.yaml` — список репозиториев (можно несколько):

```yaml
github:
  allowed_repos:
    - example-org/example-repo
    # - example-org/another-repo
```

События из репозиториев вне списка сервис игнорирует (`202`, `repo_not_allowed`).

## 4. Проверка

После `docker compose up` и публичного HTTPS (или туннеля):

1. Настройте GitHub Action по [примеру](../../examples/github/ai-review-trigger.yml), используя HTTPS URL сервиса и `AI_REVIEW_API_KEY` в GitHub Secret.
2. Откройте тестовый PR в установленном репо. В логах worker появится `review_run`; в PR — sticky comment `<!-- open-pr-review -->`.
3. Статус прогона:

```bash
curl -H "Authorization: Bearer $AI_REVIEW_API_KEY" \
  https://review.example.com/api/v1/reviews/<review_run_id>
```

Вызов из GitHub Actions (фильтры `paths` / `branches`, секреты): [OPERATIONS.md](OPERATIONS.md) и [examples/github/ai-review-trigger.yml](../../examples/github/ai-review-trigger.yml).

## Параллельные PR

Каждый запуск кладёт отдельный job в очередь. Worker ARQ берёт до `WORKER_MAX_JOBS` заданий сразу (по умолчанию **4**). Разные PR обрабатываются параллельно.

При последовательной доставке повтор того же `head_sha` переиспользует существующий прогон. Новый SHA запрашивает отмену незавершённого задания. Защита от одновременной доставки и проверка SHA перед публикацией входят в [roadmap](ROADMAP.md).

Чтобы поднять параллелизм: увеличьте `WORKER_MAX_JOBS` и/или количество контейнеров `worker` в Compose. Упираетесь в лимиты OpenAI и клонирование репозиториев, не в FastAPI.

## Частые ошибки

| Симптом | Что проверить |
| --- | --- |
| `401 invalid access key` на webhook | Текущий обработчик требует API-ключ; до реализации подписанного webhook используйте Action или ручной API. |
| `401` при валидном ключе, событие от App | `GITHUB_WEBHOOK_SECRET` на App и на сервере одинаковый; GitHub шлёт `X-Hub-Signature-256`. |
| Вебхук 202, комментария нет | Worker запущен; репозиторий в `allowed_repos`; PR не draft/fork; автор с write access. |
| Clone/comment 403 | Git clone и REST — разные протоколы. Для clone у fine-grained PAT GitHub часто требует Contents **Read and write**, даже если мы только читаем SHA. Комментарий — отдельно: Pull requests **write**. |
