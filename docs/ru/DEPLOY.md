# Спецификация: деплой Open PR Review

Задача админу: поднять **один** инстанс сервиса ревью PR и подключить его к GitHub и к уже существующему Prometheus/Grafana. Grafana, Prometheus и Loki **в этот сервис не ставятся**.

Связанные документы: [GITHUB_SETUP.md](GITHUB_SETUP.md), [OPERATIONS.md](OPERATIONS.md), [OBSERVABILITY.md](../OBSERVABILITY.md).

## Цель

На внутреннем хосте (VM / docker host) крутится Compose-стек. Открытый PR в allowlist-репозитории получает sticky-комментарий от AI Review. Worker виден в Grafana через scrape метрик.

## Не входит в задачу

- Поднять Grafana / Prometheus / Loki из этого репозитория.
- Писать логи в Loki из приложения (только stdout; ваш агент).
- Blocking GitHub Check Run.
- Создание задач в Jira (сервис только комментирует / двигает статус, если включено).

## Что деплоится

Compose из корня репозитория (`docker-compose.yml`):

| Сервис | Роль | Порт внутри | Публиковать наружу |
| --- | --- | --- | --- |
| `postgres` | состояние ревью | 5432 | **нет** (только docker network) |
| `redis` | очередь ARQ | 6379 | **нет** |
| `migrate` | `alembic upgrade head`, one-shot | — | нет |
| `api` | FastAPI | 8000 | `127.0.0.1:8000` → HTTPS reverse proxy |
| `worker` | Codex + clone + комментарий | 9100 | `127.0.0.1:9100` для локального scrape; для удалённого — внутренняя сеть |

Compose не публикует порты Postgres и Redis на хост. API и метрики worker доступны только через `127.0.0.1`; для внешнего HTTPS и удалённого Prometheus настройте reverse proxy или закрытую сеть.

Worker **обязан** стартовать с `security_opt: seccomp:unconfined` и `apparmor:unconfined` (уже в Compose). Без этого Codex/`bwrap` в Docker падает.

## Что нужно получить до начала

От команды продукта / владельца сервиса:

| Артефакт | Зачем |
| --- | --- |
| Исходники этого репозитория (ветка для деплоя) | Compose и `observability/` |
| Список `owner/repo` для ревью | `config.yaml` → `github.allowed_repos` |
| `OPENAI_API_KEY` и подтверждение, что на org **есть кредиты** | Codex (`CODEX_API_KEY` копируется из этого ключа). Без кредитов ревью падает `CODEX_QUOTA` за ~30 с |
| GitHub: **либо** fine-grained PAT, **либо** доступ создать GitHub App | clone + PR comment |
| Jira (если нужно): URL, email сервис-аккаунта, API token, проект → `rework_status` | иначе в `config.yaml` выключить `jira_comment` / `jira_transition` |
| Публичный HTTPS hostname, куда GitHub/Actions достучатся | например `https://review.internal.example.com` |
| Куда скрейпить метрики | IP/DNS api:8000 и worker:9100 с ноды Prometheus |

PAT (если без App), repository permissions:

- **Contents**: Read and write (для `git clone` по HTTPS GitHub часто требует write даже на чтение SHA)
- **Pull requests**: Read and write (sticky comment)
- Доступ ко всем репо из allowlist

## Ресурсы хоста (ориентир)

- Docker Engine + Compose v2.
- Исходящий HTTPS: `github.com`, `api.github.com`, `api.openai.com` (и Jira, если включено).
- Диск: volume `postgres_data` + временные clone на worker (сотни МБ на job). Не ставить worker на tiny диск.
- CPU/RAM: Codex тяжёлый; для 4 параллельных job (`WORKER_MAX_JOBS=4`) закладывать несколько vCPU и ≥8 GB RAM на хост, иначе резать параллелизм.

## Сеть и TLS

1. Снаружи (интернет / GitHub): **только** API по HTTPS. Терминировать TLS на nginx/Caddy/ingress.
2. Проксировать `/` → `api:8000`. Не светить `/metrics` в интернет (закрыть location или ACL).
3. `GET /health`, `GET /is-ready` — можно оставить для probe (без секрета).
4. Worker `:9100` — только Prometheus / внутренняя сеть. Не публиковать в интернет.
5. GitHub webhook / Actions ходят на `https://<host>/api/v1/…`. Самоподписанный сертификат GitHub не примет.

## Конфиг на хосте

```bash
cp .env.example .env
cp config.example.yaml config.yaml
# заполнить, не коммитить
```

### `.env` (секреты)

Сгенерировать `POSTGRES_PASSWORD` и `REVIEW_API_KEY` (`openssl rand -hex 32` для каждого). Пароль Postgres должен состоять из URL-безопасных символов, так как Compose подставляет его в `DATABASE_URL`. `REVIEW_API_KEY` — **не** GitHub PAT.

Обязательно:

```bash
POSTGRES_PASSWORD=<сгенерированный hex-пароль>
REVIEW_API_KEY=<сгенерированный ключ>
OPENAI_API_KEY=sk-...          # также используется как CODEX_API_KEY внутри sandbox
GITHUB_TOKEN=github_pat_...    # если нет App; иначе App-поля ниже
WORKER_MAX_JOBS=4
WORKER_JOB_TIMEOUT=900
LOG_LEVEL=INFO
LOG_FORMAT=text                # json — только если ваш агент логов ждёт JSON
METRICS_PORT=9100
```

GitHub App (вместо PAT): `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, опционально `GITHUB_INSTALLATION_ID=0`.

Jira: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`. Slack по умолчанию выключен (`SLACK_BOT_TOKEN` пустой, `slack.enabled: false`).

Compose формирует `DATABASE_URL` для `migrate`, `api` и `worker` из `POSTGRES_PASSWORD`; `REDIS_URL` остаётся внутренним адресом. Для локального Python без Compose задайте `DATABASE_URL` отдельно. Не публикуйте эти URL.

### `config.yaml`

```yaml
github:
  allowed_repos:
    - example-org/example-repo
    # остальные owner/name

features:
  jira_comment: true    # false, если Jira не подключаем
  jira_transition: true
  digest_enabled: true
```

После правки YAML — recreate `api` и `worker` (конфиг кэшируется).

Для своего промпта скопируйте `prompts/review.md` в `prompts/local/review.md`, отредактируйте копию и укажите `codex.prompt_file: prompts/local/review.md` в `config.yaml`. Каталог `prompts/local/` игнорируется Git; Compose монтирует `./prompts` в worker только для чтения. Путь внутри контейнера отсчитывается от `/app`. Отсутствующий файл вызывает явную ошибку ревью. После изменения пути перезапустите worker; изменение содержимого файла применяется к следующему запуску без пересборки.

## Шаги деплоя

1. Клон репозитория на хост, файлы `.env` и `config.yaml` как выше.
2. Проверить, что Postgres/Redis не опубликованы на хосте; `8000` доступен reverse proxy через loopback, `9100` — только локальному или внутреннему Prometheus.
3. `docker compose up -d --build`. Дождаться `migrate` (exit 0), `api` и `worker` Up.
4. Проверки с хоста / внутренней сети:

```bash
curl -sS https://review.example.com/health      # {"status":"ok"} или аналог
curl -sS https://review.example.com/is-ready    # ready, иначе Postgres/Redis
curl -sS http://<worker>:9100/metrics | grep open_pr_review
```

5. Reverse proxy + TLS. Проверить, что снаружи `/metrics` **не** открыт.
6. Подключить GitHub (один из вариантов).
7. Подключить Prometheus/Grafana.
8. Смоук-тест на реальном PR.

## GitHub: как события доходят до сервиса

Сервис **сам репозитории не поллит**. Нужен вход.

### Вариант A — GitHub App для доступа к API

Полный чеклист: [GITHUB_SETUP.md](GITHUB_SETUP.md).

App можно использовать для получения installation token без прямого webhook. Для текущего MVP запускайте ревью через Action или вручную. Прямую доставку webhook включать после обязательной проверки подписи; ключ доступа в URL не помещать.

### Вариант B — GitHub Actions в каждом репозитории с кодом

Используйте этот триггер для MVP, в том числе с GitHub App; он поддерживает фильтры `paths` / `branches`.

1. Репо в `allowed_repos`.
2. Org/repo secret `AI_REVIEW_API_KEY` = `REVIEW_API_KEY`.
3. Variable `AI_REVIEW_URL` = `https://review.example.com` (без `/` в конце).
4. Скопировать [examples/github/ai-review-trigger.yml](../../examples/github/ai-review-trigger.yml) в продуктовый репо как `.github/workflows/ai-review.yml`.
5. Пример уже ссылается на `maxsharlaev/autoreview-bot/.github/workflows/ai-review-trigger.yml@main`. Если Actions не видит этот репозиторий — вставить `steps` из `.github/workflows/ai-review-trigger.yml` вместо `uses:`.
6. Раскомментировать и выставить `on.pull_request.branches` / `paths` / `paths-ignore` под продукт.

CI **этого** репозитория (`ci.yml`) ревью не запускает — только тесты сервиса.

Фильтры Action и allowlist сервиса — разные слои. Пустой Action при зелёном API: смотреть `paths`/`branches`/draft.

После внедрения прямого webhook выбирайте один автоматический триггер. Одновременная доставка одного SHA пока может привести к двум запускам.

## Наблюдаемость (ваш стек)

Приложение **не** шлёт метрики и логи наружу. Скрейпер ходит **к нам**.

1. Prometheus: влить [observability/prometheus-scrape.yml](../../observability/prometheus-scrape.yml). Targets: `api:8000` и `worker:9100` (или host/IP, если scrape снаружи Docker-сети). Job names оставить `open-pr-review-api` / `open-pr-review-worker`.
2. Rule file: [observability/alerts.yml](../../observability/alerts.yml) — `OpenPrReviewWorkerDown`, `OpenPrReviewApiDown`, `OpenPrReviewCodexQuota`, `OpenPrReviewQueueNotDraining`, `OpenPrReviewStaleReviews`.
3. Grafana: Import [observability/grafana/dashboards/open-pr-review.json](../../observability/grafana/dashboards/open-pr-review.json). Datasource UID по умолчанию `prometheus` и `loki` — перенаправить на ваши.
4. Логи: ваш агент (Promtail/Alloy) читает stdout контейнеров `api` и `worker`. Метки: Compose уже ставит `OPEN_PR_REVIEW_SERVICE`. Loki-клиента в сервисе **нет**; панель логов в дашборде пустая, пока агент не пишет в ваш Loki.

`GET /api/v1/ops/status` (Bearer `REVIEW_API_KEY`) — снимок очереди без Grafana.

## Критерии приёмки

Отметить все пункты:

- [ ] `GET /health` и `GET /is-ready` с hostname сервиса отвечают успешно.
- [ ] Снаружи нельзя открыть `/metrics` и порты Postgres/Redis.
- [ ] Prometheus скрейпит api и worker (`up{job="open-pr-review-worker"} == 1`).
- [ ] Дашборд импортирован; алерты загружены в Prometheus.
- [ ] На тестовом **open, non-draft** PR из allowlist:
  - в логах worker: `worker ready` → `job start` → `codex start` → `job done` / `review_finished`;
  - в PR появился sticky comment с маркером `<!-- open-pr-review -->`;
  - `GET /api/v1/ops/status` без `CODEX_QUOTA` / `CODEX_AUTH` в последних ошибках.
- [ ] Последовательный повтор того же SHA переиспользует запуск (webhook/Action `force: false`); параллельную доставку проверить отдельно после реализации атомарной дедупликации.
- [ ] `docker compose restart worker` не уничтожает очередь; `docker compose down -v` **не** выполнять на проде без бэкапа volume.

Если комментария нет, а API 202: [OPERATIONS.md](OPERATIONS.md) (`ops/status`, квота OpenAI, PAT Contents, allowlist).

## Несколько owner'ов

Чтобы обслуживать ещё одну организацию GitHub тем же деплоем, добавьте блок `owners:` в `config.yaml` и переменные `OWNER_<ID>_*` в `.env`, затем пересоздайте `api` и `worker`. До этого ничего не меняется: обновлённый деплой без `owners:` работает с одним owner'ом `default` и пишет в логи `owner=default`. Режимы A–D, маршрутизация, имена секретов, Jira/Slack и политики на owner'а, ключи модели, а также пошаговое обновление и откат описаны в [README](../../README.md#multiple-owners); подключение второй организации — в [GITHUB_SETUP.md](GITHUB_SETUP.md).

Откат: если убрать блок `owners.<id>` и перезапустить сервис, работа этого owner'а останавливается, его записи в БД сохраняются. Чтобы вернуться к образу без поддержки нескольких owner'ов, сначала выполните новым образом `alembic downgrade 004_finding_details_visibility`. Откат `007_findings_owner_id` отказывается выполняться, пока у findings разных owner'ов совпадает stable id в одном PR; с `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1` остаётся строка owner'а репозитория (или самая старая), остальные удаляются вместе с переходами. Перед любым откатом схемы сделайте бэкап volume Postgres.

## Эксплуатация после выкладки

| Действие | Как |
| --- | --- |
| Рестарт worker | `docker compose restart worker` — очередь в Redis жива |
| Смена `.env` / `config.yaml` | `docker compose up -d --force-recreate api worker` |
| Бэкап | volume `postgres_data`; Redis — очередь, не source of truth |
| Катастрофа | `down -v` трёт БД и очередь |

On-call: [OPERATIONS.md](OPERATIONS.md).
