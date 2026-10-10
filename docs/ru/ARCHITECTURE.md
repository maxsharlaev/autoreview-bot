# Архитектура AI Review Service

Advisory MVP: GitHub webhook → очередь → Codex CLI → sticky-comment. Без blocking check и без создания Jira-задач. План развития: [ROADMAP.md](ROADMAP.md).

## Поток

```text
GitHub PR event
  -> POST /api/v1/pull-request (обязательная подпись HMAC, без API key)
  -> Redis / ARQ (до WORKER_MAX_JOBS параллельных job)
  -> Review worker
       -> GitHub App API (PR, diff, permissions)
       -> Jira read (ключ из branch/title/body)
       -> Context builder (лимиты, untrusted-блоки)
       -> git clone head SHA (PAT / installation token via GIT_ASKPASS)
       -> codex exec --sandbox read-only --output-schema
       -> JSON verifier
       -> Postgres (runs / findings / transitions)
       -> Publisher: sticky comment
       -> Jira comment + transition (P0/P1)
       -> Slack (если enabled)
Scheduler
  -> дайджест открытых PR
  -> Slack или только запись в DB
```

## Компоненты

| Компонент | Роль |
| --- | --- |
| `app/api/v1/pull_request.py` | Ingress webhook, идемпотентность, cancel старого SHA |
| `app/adapters/github.py` | GitHub App JWT, installation token, PR/diff/comments |
| `app/adapters/jira.py` | Чтение issue, комментарий, переход статуса |
| `app/adapters/slack.py` | Каркас; по умолчанию выключен |
| `app/services/orchestrator.py` | Пайплайн одного `review_run` |
| `app/services/codex_runner.py` | Subprocess Codex с урезанным env |
| `app/services/verifier.py` | JSON Schema, path/line, cap findings |
| `app/services/publisher.py` | Sticky comment, Jira write, Slack |
| `app/workers` | ARQ jobs + hourly digest, metrics on `:9100` |
| `GET /metrics`, `GET /api/v1/ops/status` | Prometheus + снимок очереди/ошибок |

## Наблюдаемость

Prometheus: API `:8000/metrics`, worker `:9100/metrics`. Стек Grafana/Prometheus/Loki в этот Compose **не входит** — внешний Prometheus сам скрейпит эндпойнты. Артефакты импорта: `observability/`. Документы: [OBSERVABILITY.md](../OBSERVABILITY.md), [OPERATIONS.md](OPERATIONS.md).

## Идемпотентность

Ключ `{repository}:{pr_number}:{head_sha}`. При последовательной доставке повтор webhook с тем же SHA переиспользует `pending` / `running` / `completed`; `failed` можно повторить. Новый SHA запрашивает отмену прежнего job. Ручной `POST /api/v1/reviews` по умолчанию `force: true` — новый run на тот же SHA. Разные PR обрабатываются параллельно (`WORKER_MAX_JOBS`). Атомарная дедупликация и проверка актуального SHA перед публикацией входят в roadmap.

## Авторизация API

`POST /api/v1/reviews`, `GET /api/v1/reviews/{id}` и `GET /api/v1/ops/status` требуют `REVIEW_API_KEY`. `GET /metrics` открыт для внутреннего scrape (не публиковать наружу). `POST /api/v1/pull-request` принимает только запросы с верной подписью `X-Hub-Signature-256` (API-ключ не нужен); без валидного webhook-секрета эндпойнт отвечает 503, а Actions и ручной API продолжают работать. Ручной запуск без вебхука: `POST /api/v1/reviews`. Установка App и секретов: [GITHUB_SETUP.md](GITHUB_SETUP.md).

## Модель данных

- `repositories` — GitHub full name, enabled
- `pull_requests` — number, SHAs, issue key, author, assignee, state
- `review_runs` — trigger, SHAs, status, versions, duration, error code
- `findings` — stable id, severity, location, evidence
- `finding_transitions` — new / resolved / still_open / regressed / obsolete / needs_human
- `task_snapshots` — Jira key и факт comment/transition
- `digest_runs` — снимок дайджеста

Raw diff в БД не хранится.

## Пропуски и ошибки

- Fork PR и draft — `skipped`
- Автор без write access — `skipped`
- Репозиторий не в непустом allowlist — `skipped`; пустой список пока разрешает любой репозиторий
- Слишком большой PR — comment `PR_TOO_LARGE_FOR_AI_REVIEW`
- Нет Jira-ключа — warning, ревью кода всё равно
- Jira недоступна — `ISSUE_CONTEXT_UNAVAILABLE`
- Невалидный JSON Codex — один retry, затем `AI_OUTPUT_INVALID`

## Язык комментария

По умолчанию summary и details на английском. `language.summary` задаёт язык summary и title; `language.details` — язык scenario, evidence, recommendation и подписей PR-комментария. Поддерживаются `en` и `ru`.

## Несколько owner'ов

Модель owner'ов живёт в `app/owners/`. `OwnerRegistry.build()` один раз при старте API и worker читает env и `config.yaml`, проверяет конфиг и строит для каждого активного owner'а неизменяемый `OwnerContext`: креды GitHub, webhook-секрет, API-ключ, привязки Jira и Slack, ключ модели и эффективный конфиг owner'а. Глобальные настройки для кредов читаются только там; адаптеры (`GitHubAppClient.from_credentials`, `JiraClient.from_binding`, `SlackClient.from_binding`, `Publisher.from_context`) получают привязки owner'а. Конфиг без `owners:` даёт одного owner'а `default`, его эффективный конфиг совпадает с глобальным (режимы A–D описаны в [README](../../README.md#multiple-owners)).

- **Маршрутизация.** `resolve()` выбирает явно указанного owner'а, затем точный `org/repo`, затем `org/*`, затем installation id из вебхука и в конце `routing.unclaimed`; после этого применяется allowlist выбранного owner'а. Корневой вебхук проверяет подпись секретами всех owner'ов и отклоняет репозиторий другого owner'а с `owner_signature_mismatch`; `/api/v1/pull-request/<id>` проверяет только секрет этого owner'а.
- **Эффективный конфиг.** Интеграции (`jira`, `slack`) берутся только из собственных блоков owner'а. Секции политик (`public_repos`, `language`, `features`, `pr_description`, `size_guard`, `codex.model`/`reasoning_effort`) глубоко сливаются поверх глобальных. Worker, публикация, size guard, описание PR, проверка метки в вебхуке и дайджест читают эффективный конфиг owner'а запуска.
- **Данные.** В `repositories`, `review_runs`, `digest_runs` и `findings` есть `owner_id` (миграции `005`–`007`); авторы комментариев хранятся вместе с owner'ом. Worker строит контекст по `review_runs.owner_id` и пропускает запуски, чей owner удалён, отключён или сменился (`owner_not_configured`, `owner_disabled`, `owner_changed`). Записи, сделанные до поддержки нескольких owner'ов, принадлежат `default` или owner'у с `aliases: [default]`.
- **Изоляция.** Codex получает только ключ модели owner'а запуска (`OPENAI_API_KEY`/`CODEX_API_KEY`) в окружении по белому списку; git получает только GitHub-токен owner'а. Дайджест строится по owner'ам: свои креды GitHub, свой `DigestRun` и свой канал Slack.
