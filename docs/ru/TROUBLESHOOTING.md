# Эксплуатация и диагностика

Цель этой страницы: быстро понять, **жив ли worker и почему ревью не появляется на PR**, не читая весь вывод Codex. Первый деплой: [DEPLOY.md](DEPLOY.md). Дашборды и алерты: [OBSERVABILITY.md](OBSERVABILITY.md).

## Как запускаются ревью

Сервис не опрашивает GitHub. Его должен кто-то вызвать.

| Триггер | Когда | Что настроить |
| --- | --- | --- |
| GitHub Actions в каждом ревьюируемом репозитории | Проще всего; поддерживает фильтры путей и веток | Workflow ниже и две настройки репозитория/организации. **Не** `ci.yml` этого репозитория. |
| Прямой webhook GitHub | Webhook App или репозитория, подписанный `X-Hub-Signature-256` | [GITHUB_SETUP.md](GITHUB_SETUP.md) |
| Ручной `POST /api/v1/reviews` | Отладка, скрипты | [API.md](API.md) |

`ci.yml` этого репозитория запускает только lint, тесты и сборку Docker. Он не запрашивает и не должен запрашивать ревью.

### GitHub Actions в ревьюируемом репозитории

1. Добавьте репозиторий в `github.allowed_repos` (или `allowed_repos` owner'а), иначе API ответит `403 repo_not_allowed`.
2. Скопируйте [examples/github/ai-review-trigger.yml](../../examples/github/ai-review-trigger.yml) в `.github/workflows/ai-review.yml` этого репозитория.
3. Задайте в репозитории или организации:

   | Имя | Вид | Значение |
   | --- | --- | --- |
   | `AI_REVIEW_API_KEY` | secret | То же значение, что `REVIEW_API_KEY` на сервере (или ключ owner'а) |
   | `AI_REVIEW_URL` | variable | `https://review.example.com`, без `/` в конце |

4. Пример использует `maxsharlaev/autoreview-bot@main`. Для форка или если Actions не может прочитать этот репозиторий, измените `uses:` или скопируйте `steps` из [`.github/workflows/ai-review-trigger.yml`](../../.github/workflows/ai-review-trigger.yml) в вызывающий workflow.
5. Job вызывает `POST /api/v1/reviews` с `pull_url` и `force: false`. HMAC webhook не участвует.

Фильтры `on.pull_request` решают, **какие PR вообще вызывают сервис**; их нет в `.env` сервиса:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
    branches:          # the PR base branch (merge target)
      - main
      - develop
    paths:             # review unless only docs changed
      - "**"
      - "!docs/**"
      - "!**/*.md"
```

- Workflow запускается, если хотя бы один изменённый файл подходит под шаблоны `paths`.
- Порядок шаблонов важен: более поздний переопределяет более ранний. Здесь `**` подходит под всё, а шаблоны с `!` затем исключают документацию, поэтому PR только с документацией пропускается, а PR, затрагивающий `src/` и `docs/`, запускается.
- GitHub не разрешает `paths` и `paths-ignore` для одного события: используйте одно из них, комбинируя включение и исключение шаблонами с `!` в `paths`.
- `branches` фильтрует **base**-ветку, а не feature-ветку.
- Пример пропускает draft через `if:`; `ready_for_review` запускает его, когда draft помечен готовым.
- Пример пропускает форки (`head.repo != github.repository`).
- `workflow_dispatch` позволяет запустить его вручную с `pull_url`.

Изменение фильтров — это коммит в ревьюируемый репозиторий; перезапуск worker'а не нужен. Использовать и Actions, и прямой webhook в одном репозитории безопасно (запросы по одному PR сериализуются, тот же SHA переиспользуется, см. [API.md](API.md#гарантии-конкурентности)), но это удваивает вызовы; лучше один триггер.

## 60 секунд без Grafana

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/is-ready
curl -fsS http://localhost:8000/metrics | grep open_pr_review
docker compose exec worker python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:9100/metrics').read().decode()[:200])"
docker compose ps
docker compose logs -f worker
```

Очередь и недавние ошибки (операторский ключ):

```bash
curl -fsS -H "Authorization: Bearer $REVIEW_API_KEY" http://localhost:8000/api/v1/ops/status
```

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/ops/status" -Headers @{ Authorization = "Bearer $env:REVIEW_API_KEY" }
```

Поле `hints` — диагноз простыми словами: нет квоты модели, нет ключа, job в очереди, но ни одна не выполняется (worker не разбирает очередь), зависшие pending/running прогоны.

## Что должно быть зелёным

| Проверка | Хорошо | Плохо |
| --- | --- | --- |
| `GET /health` | `ok` | API не стартовал |
| `GET /is-ready` | `ready` | Postgres или Redis недоступны |
| `up{job="open-pr-review-worker"}` | 1 | Worker мёртв или `:9100` не скрейпится |
| `open_pr_review_arq_queue_jobs` | 0 или падает | Растёт, пока ничего не выполняется |
| `open_pr_review_review_stale_in_flight` | 0 | Прогон pending/running дольше 15 мин |
| Последний `error_code` | пусто или пропуск | `CODEX_QUOTA`, `CODEX_AUTH`, `GIT_*` |

Строки лога worker'а, которые стоит искать: `worker ready`, `queued review_run=`, `job start`, `codex start`, `job done`, `review_finished`, `failed: CODEX_QUOTA`. Worker также пишет heartbeat Codex каждые 20 с. Для корреляции используйте id прогона.

## Симптом → действие

### API отвечает `202`, на GitHub ничего не появляется

1. `GET /api/v1/ops/status`.
2. `queue_jobs > 0` и `in_flight.running = 0`: **worker не разбирает очередь.** Контейнер запущен (`docker compose ps`)? В логе есть `worker ready` или он упал при старте (ошибка конфига owner'а останавливает worker с кодом выхода 1)? Отвечает ли `:9100/metrics`?
3. `last_failures[].error_code = CODEX_QUOTA`: **нет квоты модели.** Ревью не висит; оно падает примерно за 30–40 с. Пополните billing организации ключа. До этого повторные запросы только добавляют упавшие прогоны.
4. `CODEX_AUTH`: ключ не доходит до песочницы. Проверьте `OPENAI_API_KEY` (или ключ owner'а) в окружении **worker'а**, затем пересоздайте worker.
5. Сразу `status=completed` на том же SHA: прогон переиспользован (webhook или `force: false`). Ручной API по умолчанию использует `force: true`; пример Actions отправляет `force: false`.
6. `GIT_FORBIDDEN` / `403` при clone: доступ PAT к Contents или установка App; см. [GITHUB_SETUP.md](GITHUB_SETUP.md).
7. `GIT_REF_NOT_FOUND`: ветка удалена, SHA или ref PR больше нет.
8. Action не запускается: проверьте `on.pull_request.paths` / `branches`, draft, fork. Job выполняется, но API отвечает `401`: `AI_REVIEW_API_KEY`. `403`: репозиторий не разрешён. Проверьте, что у `AI_REVIEW_URL` нет `/` в конце.
9. Пропуск: `skip_reason` в `GET /api/v1/reviews/{id}` (draft, fork, автор без права write, size guard, причины owner'а). Пропуски не публикуют полное ревью.

### Worker «висит» в Codex (heartbeat `codex still running`)

Нормально до `codex.timeout_seconds` (по умолчанию 600 с). После `WORKER_JOB_TIMEOUT` (по умолчанию 900 с) ARQ отменяет job, и прогон становится `REVIEW_TIMEOUT`. `stale_in_flight > 0` дольше 15 минут означает, что прогон остался `running` (например, процесс умер). Пересоздайте worker; следующий запрос для этого SHA заменит зависший прогон, а job очистки закроет его статус прогресса.

### Прогон `completed`, sticky-комментария нет

Проверьте `skip_reason` / `error_code` в `GET /api/v1/reviews/{id}`. Проверьте право Pull requests **write** у PAT/App и строки лога publisher'а. Переиспользованный прогон повторно не публикует.

### `pr_queue_busy` (503)

Другой запрос по тому же PR держал блокировку PR 20 с (обычно медленное чтение GitHub или зависшая база). Ничего не изменилось. Повторите запрос или доставьте webhook повторно из «Recent deliveries» App или репозитория.

### Нет дашборда или метрик в Grafana

Prometheus должен достучаться до `:8000/metrics` и `:9100/metrics` (сеть, security groups, scrape job из [`observability/prometheus-scrape.yml`](../../observability/prometheus-scrape.yml)). Дашборд импортируется вручную из JSON; алерты из [`observability/alerts.yml`](../../observability/alerts.yml) загружаются один раз в ваш Prometheus.

### Метрики есть, логов в Grafana нет

Сервис пишет только в stdout; отправляет их ваш агент логов. Локально: `docker compose logs -f worker`.

## Коды ошибок и пропусков

| Код | Вид | Значение |
| --- | --- | --- |
| `draft_pr` | пропуск | Draft PR. |
| `fork_pr` | пропуск | PR из форка. |
| `author_no_write_access` | пропуск | У автора нет права write, maintain или admin. |
| `repo_not_allowed` | пропуск | Репозитория нет в allowlist. |
| `PR_TOO_LARGE_FOR_AI_REVIEW` | пропуск / уведомление | Превышен жёсткий порог size guard или `codex.max_files` (все изменённые файлы, включая пропущенные) / `max_diff_lines`. |
| `owner_not_configured`, `owner_disabled`, `owner_changed` | пропуск | Owner прогона удалён, отключён или репозиторий теперь маршрутизируется в другое место. |
| `ISSUE_CONTEXT_UNAVAILABLE` | предупреждение | Jira не удалось прочитать; ревью кода всё равно выполняется. |
| `AI_OUTPUT_INVALID` | ошибка | Невалидный JSON модели после одного повтора. |
| `CODEX_QUOTA` | ошибка | Квота или billing модели. Не повторяется. |
| `CODEX_AUTH` | ошибка | Ключ модели отсутствует или отклонён. Не повторяется. |
| `CODEX_TIMEOUT` | ошибка | Один вызов Codex превысил `codex.timeout_seconds`. |
| `CODEX_UNAVAILABLE` | ошибка | Codex не запустился или вернул неклассифицированную ошибку. |
| `GIT_FORBIDDEN` | ошибка | Clone запрещён. |
| `GIT_REF_NOT_FOUND` | ошибка | Коммита или ref больше нет. |
| `GITHUB_UNAVAILABLE` | ошибка | Сбой API GitHub или clone во время прогона. |
| `HEAD_CHANGED` | ошибка (статус `cancelled`) | Head PR сдвинулся до взятия в работу или до публикации ревью приватного репозитория. |
| `QUEUE_UNAVAILABLE` | ошибка | Не удалось поставить в Redis. |
| `REVIEW_TIMEOUT` | ошибка | Job превысила `WORKER_JOB_TIMEOUT`. |
| `INTERNAL_ERROR` | ошибка | Неожиданная ошибка или прогон, чья job потеряна. |

## Перезапуски и данные

| Действие | Как |
| --- | --- |
| Перезапустить worker | `docker compose restart worker`; очередь в Redis сохраняется. |
| Применить изменения `.env` / `config.yaml` | `docker compose up -d --force-recreate api worker` |
| Резервная копия | Том `postgres_data`. Redis хранит очередь, а не источник истины. |
| Никогда на production без резервной копии | `docker compose down -v` удаляет базу и очередь. |

Перед обновлением схемы сделайте резервную копию Postgres ([UPGRADE.md](UPGRADE.md)).
