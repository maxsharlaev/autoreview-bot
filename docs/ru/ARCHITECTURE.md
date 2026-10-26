<!-- section: architecture -->
# Архитектура

Autoreview-bot — рекомендательный сервис: событие PR в GitHub → очередь → Codex CLI → проверенный JSON → один обновляемый комментарий в PR. Он не блокирует merge и не создаёт задачи в Jira. См. [роадмап](ROADMAP.md) и [английскую версию](../ARCHITECTURE.md).

<!-- section: pipeline -->
## Конвейер

```text
GitHub PR event or Actions job
  -> POST /api/v1/pull-request (HMAC signature, no API key)
     or POST /api/v1/reviews (API key)
  -> Redis / ARQ (up to WORKER_MAX_JOBS jobs in parallel)
  -> Review worker
       -> GitHub API (PR, diff, author permission)
       -> Jira read (key from branch, title or body)
       -> Context builder (limits, untrusted blocks)
       -> git clone at head SHA (PAT or installation token via GIT_ASKPASS)
       -> codex exec --sandbox read-only --output-schema
       -> JSON verifier
       -> Postgres (runs / findings / transitions)
       -> Publisher: sticky comment, progress status
       -> Jira comment and status transition (P0/P1)
       -> Slack (if enabled)
Scheduler
  -> digest of open PRs
  -> Slack, or only a database record
```

<!-- section: components -->
## Компоненты

| Компонент | Ответственность |
| --- | --- |
| `app/api/v1/pull_request.py` | Приём webhook, проверка подписи, маршрутизация по owner'ам |
| `app/api/v1/reviews.py` | Ручной API ревью и статус прогона |
| `app/services/review_enqueue.py` | Создание прогона, дедупликация, отмена старых SHA |
| `app/adapters/github.py` | JWT GitHub App, installation token или PAT; PR, diff, комментарии, статусы |
| `app/adapters/jira.py` | Чтение задачи, комментарий и переход статуса |
| `app/adapters/slack.py` | Необязательные уведомления; по умолчанию выключены |
| `app/owners/` | Реестр owner'ов, маршрутизация, креды и конфиг по owner'ам |
| `app/services/orchestrator.py` | Один прогон ревью |
| `app/services/codex_runner.py` | Подпроцесс Codex с ограниченным окружением |
| `app/services/verifier.py` | JSON Schema, пути замечаний, ограничение числа |
| `app/services/publisher.py` | Sticky-комментарий, запись в Jira, Slack |
| `app/services/progress_status.py` | Check run / commit status ([PROGRESS_STATUS.md](PROGRESS_STATUS.md)) |
| `app/workers` | Job'ы ARQ, дайджест, очистка статусов, метрики worker'а на `:9100` |
| `GET /metrics`, `GET /api/v1/ops/status` | Prometheus и снимок очереди/ошибок |

<!-- section: observability -->
## Наблюдаемость

Эндпойнты Prometheus: API `:8000/metrics`, worker `:9100/metrics`. Grafana, Prometheus и Loki **не** входят в этот Compose-файл; эндпойнты скрейпит ваш Prometheus. Артефакты для импорта лежат в `observability/`. См. [OBSERVABILITY.md](OBSERVABILITY.md) и [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

<!-- section: deduplication-and-concurrency -->
## Дедупликация и конкурентность

Ключ — `{repository}:{pr_number}:{head_sha}`. Повторная доставка для того же SHA переиспользует `pending`, `running` или `completed` прогон; `failed` можно повторить. Новый SHA отменяет предыдущий прогон. Ручной `POST /api/v1/reviews` по умолчанию использует `force: true` — новый прогон на том же SHA. Разные PR выполняются параллельно (`WORKER_MAX_JOBS`).

Запросы по одному PR сериализуются advisory lock'ом Postgres, ARQ job id детерминированы для каждого прогона, а worker захватывает прогон условным обновлением, поэтому конкурентные доставки не создают дублирующих прогонов. Worker перечитывает head PR перед стартом и закрывает прогон как `HEAD_CHANGED`, если он сдвинулся; для приватных репозиториев он проверяет ещё раз перед публикацией. Подробнее: [API.md](API.md#гарантии-конкурентности).

<!-- section: api-authentication -->
## Аутентификация API

`POST /api/v1/reviews`, `GET /api/v1/reviews/{id}` и `GET /api/v1/ops/status` требуют API key. `GET /metrics` предназначен для внутреннего скрейпинга; не открывайте его наружу. `POST /api/v1/pull-request` принимает только запросы с валидным `X-Hub-Signature-256` (без API key); без валидного секрета webhook он возвращает 503, а Actions и ручной API продолжают работать. См. [API.md](API.md), [GITHUB_SETUP.md](GITHUB_SETUP.md) и [SECURITY.md](../../SECURITY.md).

<!-- section: data-model -->
## Модель данных

- `repositories` — полное имя GitHub, enabled, owner, авторы комментариев
- `pull_requests` — номер, SHA, ключ задачи, автор, исполнитель, состояние, заголовок бота
- `review_runs` — триггер, SHA, статус, версии, длительность, код ошибки/пропуска, статус прогресса
- `findings` — stable id, серьёзность, расположение, доказательства, видимость
- `finding_transitions` — new / resolved / still_open / regressed / obsolete / needs_human
- `task_snapshots` — ключ Jira и был ли сделан комментарий/переход
- `digest_runs` — снимок дайджеста

Сырые diff не хранятся. Подробнее: [DATA_HANDLING.md](DATA_HANDLING.md).

<!-- section: skips-and-errors -->
## Пропуски и ошибки

- PR из форков и draft-PR: `skipped`.
- Автор без права write: `skipped`.
- Репозиторий вне непустого allowlist: `repo_not_allowed`. Пустой список разрешает любой репозиторий.
- Слишком большой PR: комментарий `PR_TOO_LARGE_FOR_AI_REVIEW`.
- Нет ключа Jira: предупреждение, код всё равно ревьюится. Jira недоступна: `ISSUE_CONTEXT_UNAVAILABLE`.
- Невалидный JSON Codex: один повтор, затем `AI_OUTPUT_INVALID`.

Все коды: [TROUBLESHOOTING.md](TROUBLESHOOTING.md#коды-ошибок-и-пропусков).

<!-- section: comment-language -->
## Язык комментария

По умолчанию вывод на английском. `language.summary` управляет summary и заголовками замечаний; `language.details` — сценарием, доказательствами, рекомендацией и подписями комментария. Оба поддерживают `en` и `ru`.

<!-- section: multiple-owners -->
## Несколько owner'ов

`app/owners/` содержит модель owner'а. `OwnerRegistry.build()` один раз при старте (API и worker) читает env и `config.yaml`, всё проверяет и строит замороженный `OwnerContext` для каждого активного owner'а: креды GitHub, секрет webhook, API key, привязки Jira и Slack, ключ модели и эффективный конфиг owner'а. Глобальные настройки читаются ради кредов только там; адаптеры (`GitHubAppClient.from_credentials`, `JiraClient.from_binding`, `SlackClient.from_binding`, `Publisher.from_context`) принимают привязки owner'а. Конфиг без `owners:` даёт одного owner'а, `default`, чей эффективный конфиг — глобальный (режимы A–D: [MULTI_OWNER.md](MULTI_OWNER.md#режимы)).

- **Маршрутизация.** `resolve()` выбирает явного owner'а, затем точную заявку `org/repo`, заявку `org/*`, id установки из webhook и наконец `routing.unclaimed`; затем применяется allowlist выбранного owner'а. Корневой webhook проверяет подпись по секретам всех owner'ов и отклоняет репозиторий другого owner'а с `owner_signature_mismatch`; `/api/v1/pull-request/<id>` проверяет только секрет этого owner'а.
- **Эффективный конфиг.** Интеграции (`jira`, `slack`) берутся только из собственных блоков owner'а. Секции политик (`public_repos`, `language`, `features`, `pr_description`, `size_guard`, `progress_status`, `codex.model`/`reasoning_effort`) глубоко сливаются с глобальными секциями. Worker, publisher, size guard, описание PR, проверка метки в webhook и дайджест читают эффективный конфиг owner'а прогона.
- **Данные.** `repositories`, `review_runs`, `digest_runs` и `findings` содержат `owner_id` (миграции `005`-`007`); авторы комментариев хранятся вместе со своим owner'ом. Worker строит контекст из `review_runs.owner_id` и пропускает прогоны, чей owner удалён, отключён или перемаршрутизирован (`owner_not_configured`, `owner_disabled`, `owner_changed`). Строки, записанные до поддержки нескольких owner'ов, принадлежат `default` или owner'у с `aliases: [default]`.
- **Изоляция.** Codex получает только ключ модели owner'а прогона (`OPENAI_API_KEY`/`CODEX_API_KEY`) в окружении из allowlist; git получает только токен GitHub owner'а. Дайджест выполняется по каждому owner'у со своими кредами GitHub, `DigestRun` и каналом Slack.
