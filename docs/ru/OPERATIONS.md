# Эксплуатация: куда смотреть и что чинить

Задача девопса — понять, **жив ли worker и почему ревью не появляется в PR**, а не читать весь stderr Codex.

Выкладка с нуля: [DEPLOY.md](DEPLOY.md).

## Настройка: как ревью вообще запускается

Сервис сам GitHub не опрашивает. Кто-то должен дернуть API.

| Способ | Когда | Что править |
| --- | --- | --- |
| GitHub Actions в репозитории с кодом | Текущий MVP | Файл ниже + секреты. **Не** `.github/workflows/ci.yml` этого репозитория |
| Прямой GitHub webhook | Подпись `X-Hub-Signature-256` обязательна | [GITHUB_SETUP.md](GITHUB_SETUP.md) |
| Ручной `POST /api/v1/reviews` | Отладка | README |

CI **этого** репозитория (`ci.yml`) — lint/тесты/docker build. Триггера ревью там нет и не должно быть.

### GitHub Actions в продуктовом репо

1. Репозиторий в `config.yaml` → `github.allowed_repos` (иначе `403 repo_not_allowed`).
2. Скопировать [examples/github/ai-review-trigger.yml](../../examples/github/ai-review-trigger.yml) в продуктовый репо как `.github/workflows/ai-review.yml`.
3. Secrets / variables **того** репо (или org):

   | Имя | Тип | Значение |
   | --- | --- | --- |
   | `AI_REVIEW_API_KEY` | secret | то же, что `REVIEW_API_KEY` на сервере |
   | `AI_REVIEW_URL` | variable | `https://review.example.com` без `/` в конце |

4. Пример использует `maxsharlaev/autoreview-bot@main`. Если используете fork или Actions **не может** читать этот репозиторий, поправьте `uses:` либо скопируйте `steps` из `.github/workflows/ai-review-trigger.yml` прямо в workflow вызывающего репозитория.
5. Job вызывает `POST /api/v1/reviews` с `pull_url`. HMAC вебхука не нужен (это не доставка App).

Фильтры GitHub Actions (`on.pull_request`) задают, **какие PR вообще дергают** сервис. Их нет в `.env` сервиса.

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
    branches:          # base branch PR (куда мержат)
      - main
      - develop
    paths:             # ревью, только если среди изменений есть эти пути
      - "src/**"
      - "apps/**"
    paths-ignore:      # не ревьюить, если изменилось только это
      - "docs/**"
      - "**/*.md"
```

- Несколько `paths` — **ИЛИ** (сработал любой путь).
- `paths` и `paths-ignore` вместе: сначала include, потом exclude. Если в PR и `src/` и `docs/`, workflow всё равно запустится.
- `branches` — это **base**, не имя feature-ветки.
- Draft пропускается `if:` в примере; `ready_for_review` после draft прогонит.
- Fork PR пример тоже режет (`head.repo != github.repository`).
- `workflow_dispatch` — ручной прогон: ввести `pull_url`.

Смена фильтров = коммит workflow в продуктовый репо. Перезапускать worker не нужно.

После включения прямого webhook выбирайте один автоматический триггер. Последовательные запросы на один SHA обычно переиспользуют прогон, но при одновременной доставке возможна гонка до её исправления в [roadmap](ROADMAP.md).

## 60 секунд без Grafana

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8000/is-ready
curl -s http://localhost:9100/metrics | findstr open_pr_review
# Linux: curl -s http://localhost:9100/metrics | grep open_pr_review
```

Снимок очереди и последних падений (тот же `REVIEW_API_KEY`, что для ручного ревью):

```powershell
$key = "<REVIEW_API_KEY>"
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/ops/status" -Headers @{ Authorization = "Bearer $key" }
```

Поле `hints` — человеческий диагноз: нет квоты OpenAI, нет ключа, очередь есть а running=0 (worker не ест job), stale pending/running.

Grafana (ваш инстанс): скрейпить `api:8000/metrics` и `worker:9100/metrics`, импорт дашборда `observability/grafana/dashboards/autoreview-bot.json`. Как подключить: [OBSERVABILITY.md](../OBSERVABILITY.md).

## Что должно быть зелёным

| Проверка | Ок | Плохо |
| --- | --- | --- |
| `GET /health` | `ok` | API не стартовал |
| `GET /is-ready` | `ready` | Postgres или Redis |
| `up{job="open-pr-review-worker"}` | 1 | Процесс worker мёртв или `:9100` не скрейпится |
| `open_pr_review_arq_queue_jobs` | 0 или падает | Растёт, `running=0` |
| `open_pr_review_review_stale_in_flight` | 0 | Job завис >15 мин |
| Последний `error_code` | пусто / skip | `CODEX_QUOTA`, `CODEX_AUTH`, `GIT_*` |

Логи worker: `docker compose logs -f worker`. Ищите `worker ready`, `queued review_run=`, `job start`, `job done`, `review_finished`, `failed: CODEX_QUOTA`.

## Симптом → действие

### «Долбим PR, в GitHub тишина, API отвечает 202»

1. `GET /api/v1/ops/status`.
2. `queue_jobs > 0` и `in_flight.running = 0` → **worker не потребляет очередь**.
   - `docker compose ps` — контейнер worker Up?
   - `docker compose logs worker` — есть `worker ready`? Упал Codex/Python при старте?
   - Порт `9100` слушается? (`curl localhost:9100/metrics`)
3. `last_failures[].error_code = CODEX_QUOTA` → **квоты OpenAI нет**. Ревью *не висит*: оно падает за ~30–40 с. Пополнить billing организации, к которой привязан `sk-proj-…` в `.env`. Пока кредитов нет, повторные POST только копят `failed`.
4. `CODEX_AUTH` → в sandbox не уходит ключ. Проверить `OPENAI_API_KEY` в `.env` worker (не только api). После смены — recreate worker.
5. `status=completed` сразу на том же SHA → **идемпотентность webhook**. Ручной `POST /api/v1/reviews` по умолчанию `force: true`. Action в примере шлёт `force: false`.
6. `GIT_FORBIDDEN` / clone 403 → Contents у PAT; см. [GITHUB_SETUP.md](GITHUB_SETUP.md).
7. `GIT_REF_NOT_FOUND` → ветка удалена и SHA/PR ref уже недоступны.
8. Action в продуктовом репо не стартует → смотрите `on.pull_request.paths` / `branches` / draft / fork. Job стартовал, API 401 → `AI_REVIEW_API_KEY`. 403 → репо не в `allowed_repos`. Variable `AI_REVIEW_URL` без хвоста `/`.

### Worker «висит» на Codex (heartbeat `codex still running`)

Это нормально до `timeout_seconds` (по умолчанию 600 с). Если дольше `WORKER_JOB_TIMEOUT` (900 с) — ARQ убьёт job. `stale_in_flight` > 0 после 15 минут без `updated_at` — зависший `running` (падение процесса без `failed`). Recreate worker; stale run при следующем enqueue того же SHA помечается `INTERNAL_ERROR`, если ARQ job уже мёртв.

### Нет sticky-комментария, статус `completed`

Проверить `skip_reason` / `error_code` на `GET /api/v1/reviews/{id}`. Skip (draft, fork, no write) не пишет полноценный review. Права PAT: Pull requests **write** на комментарий.

### В Grafana нет дашборда / нет метрик

Prometheus должен **достучаться** до `:8000/metrics` и `:9100/metrics` (сеть, security group, scrape job из `observability/prometheus-scrape.yml`). Дашборд — ручной импорт JSON. Алерты `observability/alerts.yml` — один раз в ваш Prometheus.

### В Grafana нет логов, метрики есть

Логи не идут в Loki сами — агент платформы читает stdout. Локально: `docker compose logs -f worker`.

## Как это завести с нуля

1. `.env` из `.env.example`: `REVIEW_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN` или App, `config.yaml` с `allowed_repos`.
2. `docker compose up --build`.
3. `GET /health` и `/is-ready`.
4. Ручной прогон: `POST /api/v1/reviews` (README). В логах worker — `job start` → `codex start` → `job done`.
5. Обсервабилити: ваш Prometheus скрейпит `api:8000/metrics` и `worker:9100/metrics`. Дашборд и алерты — каталог `observability/` (импорт в уже развёрнутые Grafana/Prometheus). См. [OBSERVABILITY.md](../OBSERVABILITY.md).

Прод: `/metrics` не публиковать в интернет. Алерты `OpenPrReviewCodexQuota` и `OpenPrReviewQueueNotDraining` — это «долбим PR, а оно не ревьюит».

## Рестарт без потери очереди

Redis хранит ARQ. `docker compose restart worker` безопасен. `docker compose down -v` сотрёт Postgres **и** очередь. Не делать на проде без бэкапа volume `postgres_data`.
