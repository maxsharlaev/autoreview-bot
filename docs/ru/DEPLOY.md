<!-- section: deploy-autoreview-bot -->
# Деплой autoreview-bot

Цель: запустить **один** инстанс сервиса ревью и подключить его к GitHub и к уже существующему стеку Prometheus/Grafana. Grafana, Prometheus и Loki этим сервисом **не** устанавливаются.

Связанные документы: [GITHUB_SETUP.md](GITHUB_SETUP.md), [CONFIGURATION.md](CONFIGURATION.md), [ENVIRONMENT.md](ENVIRONMENT.md), [TROUBLESHOOTING.md](TROUBLESHOOTING.md), [OBSERVABILITY.md](OBSERVABILITY.md), [SECURITY.md](../../SECURITY.md), [английская версия](../DEPLOY.md).

<!-- section: outcome -->
## Результат

Стек Compose работает на внутреннем хосте (виртуальная машина или Docker-хост). Открытый PR в репозитории из allowlist получает sticky-комментарий. Worker виден в Grafana через скрейпинг метрик.

<!-- section: out-of-scope -->
## Вне рамок

- Установка Grafana, Prometheus или Loki из этого репозитория.
- Отправка логов в Loki из приложения (только stdout; отправляет ваш агент).
- Блокирующая проверка GitHub (статус прогресса информационный, [PROGRESS_STATUS.md](PROGRESS_STATUS.md)).
- Создание задач в Jira (сервис только комментирует и переводит статус, если включено).

<!-- section: what-is-deployed -->
## Что разворачивается

Compose из корня репозитория (`docker-compose.yml`):

| Сервис | Назначение | Порт внутри | Наружу |
| --- | --- | --- | --- |
| `postgres` | Состояние ревью | 5432 | **Нет** (только Docker-сеть) |
| `redis` | Очередь ARQ | 6379 | **Нет** |
| `migrate` | Однократный `alembic upgrade head` | — | Нет |
| `api` | FastAPI | 8000 | `127.0.0.1:8000` → HTTPS reverse proxy |
| `worker` | Clone, Codex, публикация, метрики | 9100 | **Нет**: только Docker-сеть (`expose`), поэтому реплики можно масштабировать |

Compose не публикует порты Postgres и Redis. API опубликован только на `127.0.0.1:8000`, а метрики worker'а не публикуются вовсе; для внешнего HTTPS и удалённого Prometheus используйте reverse proxy или закрытую сеть.

Worker **обязан** работать с `security_opt: seccomp:unconfined` и `apparmor:unconfined` (уже есть в Compose), чтобы внутренняя песочница bubblewrap у Codex могла стартовать. Не убирайте их, не проверив на целевом хосте.

<!-- section: what-to-collect-first -->
## Что собрать заранее

| Что | Зачем |
| --- | --- |
| Этот репозиторий (ветка для деплоя) | Compose и `observability/` |
| Список `owner/repo` для ревью | `github.allowed_repos` в `config.yaml` |
| `OPENAI_API_KEY` и подтверждённые **кредиты** в его организации | Codex. Без кредитов ревью падает с `CODEX_QUOTA` примерно за 30 с |
| GitHub: fine-grained PAT **или** право создать GitHub App | Clone и комментарий в PR ([GITHUB_SETUP.md](GITHUB_SETUP.md)) |
| Jira, если нужна: URL, email сервисного аккаунта, API-токен, проект → `rework_status` | Иначе выключите `jira_comment` / `jira_transition` |
| Публичное HTTPS-имя хоста, доступное GitHub/Actions | Например `https://review.example.com` |
| Откуда скрейпятся метрики | API: IP/DNS `api:8000` с точки зрения Prometheus; worker: `worker:9100` только из Docker-сети стека (DNS-обнаружение реплик) |

<!-- section: host-resources -->
## Ресурсы хоста

- Docker Engine и Compose v2.
- Исходящий HTTPS к `github.com`, `api.github.com`, `api.openai.com` (и к Jira, если включена).
- Диск: том `postgres_data` плюс временные клоны на worker'е (сотни МБ на job).
- CPU/RAM: Codex тяжёлый. Для 4 параллельных job (`WORKER_MAX_JOBS=4`) закладывайте несколько vCPU и не меньше 8 ГБ RAM или снизьте параллелизм.

<!-- section: network-and-tls -->
## Сеть и TLS

1. Из интернета/GitHub открывайте **только** API по HTTPS. Завершайте TLS на nginx, Caddy или ingress.
2. Проксируйте `/` → `api:8000`. Не открывайте `/metrics` (заблокируйте location или используйте ACL). Рассмотрите блокировку `/docs` и `/openapi.json`; они без аутентификации.
3. `GET /health` и `GET /is-ready` не требуют секрета и могут оставаться открытыми для проб.
4. Worker `:9100`: только Prometheus/внутренняя сеть.
5. Webhooks и Actions GitHub вызывают `https://<host>/api/v1/…`. GitHub не принимает самоподписанные сертификаты.

<!-- section: configuration-on-the-host -->
## Конфигурация на хосте

```bash
cp .env.example .env
cp config.example.yaml config.yaml
# fill in, never commit
```

<!-- section: env-secrets -->
### `.env` (секреты)

Сгенерируйте `POSTGRES_PASSWORD` и `REVIEW_API_KEY` (каждый `openssl rand -hex 32`). Пароль Postgres должен быть URL-безопасным, потому что Compose подставляет его в `DATABASE_URL`. `REVIEW_API_KEY` — **не** PAT GitHub.

Обязательно:

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

GitHub App вместо PAT: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, необязательно `GITHUB_INSTALLATION_ID=0`. `GITHUB_WEBHOOK_SECRET`, если используете webhooks.

Jira: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`. Slack по умолчанию выключен (`SLACK_BOT_TOKEN` пуст, `slack.enabled: false`).

Compose собирает `DATABASE_URL` для `migrate`, `api` и `worker` из `POSTGRES_PASSWORD`; `REDIS_URL` остаётся внутренним. Для локального процесса Python задайте `DATABASE_URL` сами. Все переменные: [ENVIRONMENT.md](ENVIRONMENT.md).

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

Задайте непустой allowlist: пустой список разрешает любой репозиторий, доступный кредам. Оба поля языка принимают `en` или `ru`. После изменений YAML пересоздайте `api` и `worker`; конфигурация кэшируется. Все ключи: [CONFIGURATION.md](CONFIGURATION.md).

Свои инструкции ревью: скопируйте `prompts/review.md` в `prompts/local/review.md`, отредактируйте копию и задайте `codex.prompt_file: prompts/local/review.md`. `prompts/local/` в git-ignore; Compose монтирует `./prompts` в worker только для чтения, а путь задаётся относительно `/app` в контейнере. Сохраните в заменяющем промпте схему вывода и правила для недоверенного ввода. Отсутствующий файл явно проваливает ревью. Правки файла применяются к следующему ревью; смена пути требует перезапуска worker'а.

<!-- section: deployment-steps -->
## Шаги деплоя

1. Склонируйте репозиторий на хост; создайте `.env` и `config.yaml`, как описано выше.
2. Проверьте, что Postgres/Redis не опубликованы на хосте, `8000` доступен reverse proxy через loopback, а `9100` — только внутри Docker-сети.
3. `docker compose up -d --build`. Дождитесь `migrate` (exit 0), затем `api` и `worker` в состоянии Up.
4. Проверки с хоста или из внутренней сети:

   ```bash
   curl -sS https://review.example.com/health
   curl -sS https://review.example.com/is-ready    # "ready", otherwise Postgres/Redis
   docker compose exec worker python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:9100/metrics').read().decode()[:200])"
   ```

5. Reverse proxy и TLS. Проверьте, что `/metrics` **не** доступен снаружи.
6. Подключите GitHub (ниже).
7. Подключите Prometheus/Grafana.
8. Smoke-тест на реальном PR.

<!-- section: how-events-reach-the-service -->
## Как события доходят до сервиса

Сервис **не** опрашивает репозитории. Выберите один автоматический триггер на репозиторий.

<!-- section: option-a-github-actions-in-each-reviewed-repository -->
### Вариант A: GitHub Actions в каждом ревьюируемом репозитории

Проще всего; поддерживает фильтры `paths` / `branches`; работает с PAT или App.

1. Добавьте репозиторий в `allowed_repos`.
2. Секрет организации/репозитория `AI_REVIEW_API_KEY` = `REVIEW_API_KEY` (или ключ owner'а).
3. Переменная `AI_REVIEW_URL` = `https://review.example.com` (без `/` в конце).
4. Скопируйте [examples/github/ai-review-trigger.yml](../../examples/github/ai-review-trigger.yml) в `.github/workflows/ai-review.yml` ревьюируемого репозитория.
5. Пример вызывает `maxsharlaev/autoreview-bot/.github/workflows/ai-review-trigger.yml@main`. Если Actions не видит этот репозиторий, вставьте `steps` из `.github/workflows/ai-review-trigger.yml` вместо `uses:`.
6. Настройте `on.pull_request.branches` и либо `paths`, либо `paths-ignore`, но не оба ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#github-actions-в-ревьюируемом-репозитории)).

`ci.yml` этого репозитория ревью не запускает, только тесты сервиса. Фильтры Action и allowlist сервиса — отдельные уровни: зелёный API без запуска Action означает, что нужно проверить `paths`/`branches`/draft.

<!-- section: option-b-direct-webhook -->
### Вариант B: прямой webhook

Webhook GitHub App (или репозитория) на `/api/v1/pull-request`. Каждая доставка должна быть подписана `GITHUB_WEBHOOK_SECRET` (`X-Hub-Signature-256`). Никогда не кладите секрет в URL. Чеклист: [GITHUB_SETUP.md](GITHUB_SETUP.md).

Оба триггера на одном репозитории не создают дублирующих прогонов для одного SHA (см. [API.md](API.md#гарантии-конкурентности)), но удваивают запросы.

<!-- section: observability-your-stack -->
## Наблюдаемость (ваш стек)

Приложение **не** отправляет метрики и логи само; их забирает ваш скрейпер.

1. Prometheus: влейте [observability/prometheus-scrape.yml](../../observability/prometheus-scrape.yml). API: `api:8000` (или host/IP при скрейпинге снаружи Docker-сети). Worker: Prometheus должен быть в Docker-сети стека (или иметь явную маршрутизацию в неё); job worker'а находит все реплики через `dns_sd_configs` по имени сервиса `worker`. Чтобы скрейпить worker с хоста, добавьте `docker-compose.override.yml` с `ports: ["127.0.0.1:9100:9100"]` для `worker` (см. [UPGRADE.md](UPGRADE.md)); тогда worker нельзя масштабировать. Оставьте имена job `open-pr-review-api` / `open-pr-review-worker`.
2. Правила: [observability/alerts.yml](../../observability/alerts.yml): `OpenPrReviewWorkerDown`, `OpenPrReviewWorkerTargetDown`, `OpenPrReviewApiDown`, `OpenPrReviewCodexQuota`, `OpenPrReviewQueueNotDraining`, `OpenPrReviewStaleReviews`.
3. Grafana: импортируйте [observability/grafana/dashboards/autoreview-bot.json](../../observability/grafana/dashboards/autoreview-bot.json). UID datasource'ов по умолчанию — `prometheus` и `loki`; направьте их на свои.
4. Логи: ваш агент (Promtail/Alloy) читает stdout `api` и `worker`. Compose задаёт `OPEN_PR_REVIEW_SERVICE` (`api` / `worker`), которое попадает в поле `service` JSON-логов (`LOG_FORMAT=json`). Клиента Loki в сервисе нет; панель логов дашборда остаётся пустой, пока ваш агент не отправляет логи в Loki.

`GET /api/v1/ops/status` (Bearer `REVIEW_API_KEY`) даёт снимок очереди без Grafana.

<!-- section: acceptance-criteria -->
## Критерии приёмки

- [ ] `GET /health` и `GET /is-ready` отвечают на имени хоста сервиса.
- [ ] `/metrics` и порты Postgres/Redis недоступны снаружи.
- [ ] Prometheus скрейпит api и worker (`up{job="open-pr-review-worker"} == 1`).
- [ ] Дашборд импортирован; алерты загружены в Prometheus.
- [ ] На тестовом **открытом, не draft** PR из allowlist:
  - лог worker'а: `worker ready` → `job start` → `codex start` → `job done` / `review_finished`;
  - в PR есть sticky-комментарий с маркером `<!-- open-pr-review -->`;
  - `GET /api/v1/ops/status` не показывает `CODEX_QUOTA` / `CODEX_AUTH` среди недавних ошибок.
- [ ] Повтор того же SHA переиспользует прогон (webhook или Action с `force: false`).
- [ ] `docker compose restart worker` сохраняет очередь. Никогда не выполняйте `docker compose down -v` на production без резервной копии томов.
- [ ] [Чеклист hardening](../../SECURITY.md#deployment-hardening-checklist) выполнен.

API `202`, но комментария нет: [TROUBLESHOOTING.md](TROUBLESHOOTING.md) (`ops/status`, квота модели, PAT Contents, allowlist).

<!-- section: several-owners -->
## Несколько owner'ов

Чтобы обслуживать ещё одну организацию GitHub из того же деплоя, добавьте блок `owners:` в `config.yaml` и его переменные `OWNER_<ID>_*` в `.env`, затем пересоздайте `api` и `worker`. Пока вы этого не сделали, ничего не меняется: деплой без `owners:` сохраняет одного owner'а `default` и пишет `owner=default`. Всё остальное: [MULTI_OWNER.md](MULTI_OWNER.md).

<!-- section: after-rollout -->
## После выката

| Действие | Как |
| --- | --- |
| Перезапустить worker | `docker compose restart worker`; очередь Redis сохраняется |
| Изменить `.env` / `config.yaml` | `docker compose up -d --force-recreate api worker` |
| Резервная копия | Том `postgres_data`; Redis — очередь, а не источник истины |
| Авария | `down -v` удаляет базу и очередь |
| Обновление | [UPGRADE.md](UPGRADE.md) |

Дежурство: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
