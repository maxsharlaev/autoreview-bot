# HTTP API

API слушает порт 8000 (`127.0.0.1:8000` в Compose; перед ним нужен reverse proxy с TLS). FastAPI также отдаёт схему OpenAPI на `GET /openapi.json` и `GET /docs` без аутентификации.

| Эндпойнт | Аутентификация | Назначение |
| --- | --- | --- |
| `POST /api/v1/reviews` | API key | Запустить ревью (GitHub Actions, скрипты, вручную). |
| `GET /api/v1/reviews/{review_run_id}` | API key | Статус одного прогона. |
| `GET /api/v1/ops/status` | только операторский ключ | Снимок очереди и ошибок. |
| `POST /api/v1/pull-request` | подпись webhook | Webhook GitHub, маршрутизируется к owner'у по репозиторию. |
| `POST /api/v1/pull-request/{owner_id}` | подпись webhook этого owner'а | Webhook GitHub для одного owner'а. |
| `GET /health` | нет | Liveness: `{"status": "ok"}`. |
| `GET /is-ready` | нет | Postgres и Redis доступны: `{"status": "ready"}`. |
| `GET /metrics` | нет | Prometheus (API). Worker отдаёт свои метрики на `:9100/metrics`. Оба держите внутренними. |
| `GET /docs`, `GET /openapi.json` | нет | Сгенерированная схема API. |

## Аутентификация

Передайте ключ одним из способов:

- `Authorization: Bearer <key>`
- `X-Api-Key: <key>`
- HTTP Basic (пароль — это ключ)

Ключи:

- `REVIEW_API_KEY` — **операторский ключ**: все owner'ы, все прогоны и `ops/status`.
- `owners.<id>.api.key_env` (по умолчанию `OWNER_<ID>_REVIEW_API_KEY`) — ключ **одного owner'а**: запускает ревью только для этого owner'а и читает только его прогоны. `ops/status` ему недоступен.

Отсутствующий или неверный ключ — `401 {"detail": "invalid access key"}`. Эндпойнты webhook API key не принимают; они проверяют `X-Hub-Signature-256`. Никогда не передавайте ключ в URL.

## Выбор owner'а

При нескольких owner'ах ([MULTI_OWNER.md](MULTI_OWNER.md)) запрос маршрутизируется по репозиторию. Чтобы явно выбрать owner'а, передайте `X-Review-Owner: <owner-id>` или `?owner=<owner-id>` (без учёта регистра, алиасы принимаются). Если оба заданы с разными значениями — `400`. Ключ owner'а всегда использует своего owner'а.

## `POST /api/v1/reviews`

```bash
curl -sS -X POST https://review.example.com/api/v1/reviews \
  -H "Authorization: Bearer $REVIEW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"repository": "example-org/example-repo", "number": 1}'
```

PowerShell:

```powershell
$headers = @{ Authorization = "Bearer $env:REVIEW_API_KEY" }
$body = @{ pull_url = "https://github.com/example-org/example-repo/pull/1" } | ConvertTo-Json
Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/reviews -Headers $headers -ContentType "application/json" -Body $body
```

### Тело запроса

| Поле | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `pull_url` | string | — | `https://github.com/<owner>/<repo>/pull/<n>`. Либо это поле, либо `repository` + `number`. |
| `repository` | string | — | `owner/repo`. |
| `number` | int ≥ 1 | — | Номер PR. |
| `head_sha` | string | — | Коммит, который вы ожидаете отревьюить. Если это не текущий head PR (полный SHA, без учёта регистра), запрос отклоняется с `409 stale_head_sha` до каких-либо изменений. |
| `force` | bool | `true` | `true`: запустить новый прогон, даже если для этого SHA прогон уже есть (открытый отменяется). `false`: переиспользовать pending, running или completed прогон для того же SHA. |
| `report_status` | bool | `true` | `false`: этот прогон не публикует статус прогресса. Открытый статус заменённого прогона всё равно закрывается. |
| `head_ref`, `title`, `html_url`, `body` | string | — | Необязательные подсказки; для ревью используются значения из GitHub. |
| `base_sha` | string | — | Принимается для совместимости и игнорируется; используется текущий base из GitHub. |

Эндпойнт всегда читает PR из GitHub с кредами owner'а и привязывает прогон к его текущему head. Draft- и fork-PR здесь принимаются, но worker их пропускает.

### Ответ `202`

```json
{"status": "pending", "review_run_id": "<uuid>", "head_sha": "<sha>", "trigger": "manual", "owner": "<owner-id>"}
```

`status` может быть `running` или `completed`, если `force: false` переиспользовал прогон.

### Ошибки

| HTTP | `detail` | Значение |
| --- | --- | --- |
| 400 | текст | `X-Review-Owner` и `?owner=` не совпадают. |
| 401 | `invalid access key` | Ключ отсутствует или неверен. |
| 403 | `repo_not_allowed` | Репозитория нет в allowlist owner'а. |
| 403 | `unknown_owner` | Ни один owner не заявляет репозиторий, и `routing.unclaimed: reject`. |
| 403 | `owner_disabled` | Owner отключён (`enabled: false`). |
| 403 | `owner_forbidden` | Ключ owner'а запросил другого owner'а. |
| 404 | `unknown_owner` | Owner из заголовка/параметра не существует. |
| 404 | `owner_not_configured` | У выбранного owner'а нет конфигурации. |
| 409 | `owner_repo_conflict` | Явно указанный owner — не тот, что заявляет этот репозиторий. |
| 409 | `repo_renamed: …` | GitHub вернул новое имя репозитория, которое маршрутизируется к другому owner'у. |
| 409 | `{"code": "stale_head_sha", "head_sha": "<requested>", "current_head_sha": "<current>"}` | `head_sha` — не текущий head, в том числе если head сдвинулся, пока запрос ждал (см. ниже). Ничего не отменено, не сохранено, не поставлено в очередь и не опубликовано. |
| 422 | ошибка валидации | Нужен `pull_url` либо `repository` + `number`. |
| 502 | `github: …` | Не удалось прочитать PR из GitHub (креды, права, PR не найден, таймаут). В очередь ничего не поставлено. |
| 503 | `{"code": "pr_queue_busy"}` + `Retry-After: 5` | Другой запрос по тому же PR держал блокировку очереди 20 с. Ничего не изменилось; повторите. |

Если постановка в Redis не удалась, запрос завершается `500`, а прогон сохраняется как `failed` / `QUEUE_UNAVAILABLE`; следующий запрос его заменит.

### Гарантии конкурентности

- Постановка в очередь сериализуется по PR (репозиторий + номер) advisory lock'ом Postgres, который берёт и webhook. Запрос ждёт его не больше 20 с, затем получает `503 pr_queue_busy`.
- Если ручной запрос отменил бы открытый прогон того же owner'а на **другом** SHA, эндпойнт перечитывает head PR под блокировкой (не больше 10 с). Сдвинувшийся head даёт `409 stale_head_sha`; неудачное перечитывание — `502`.
- ARQ job id каждого прогона — `review:<run id>`, он хранится вместе с прогоном, поэтому один прогон нельзя поставить в очередь дважды. `pending`-прогон моложе 60 с, чьей job ещё нет в Redis, всё ещё считается выполняющимся; после этого он считается потерянным и заменяется.
- Worker обрабатывает только `pending`-прогоны и захватывает их условным обновлением `pending → running`, поэтому из двух конкурирующих job ревью делает только одна.
- Вытесненная job прерывается после коммита нового прогона (best effort, не больше 2 с).

## `GET /api/v1/reviews/{review_run_id}`

```json
{
  "review_run_id": "<uuid>",
  "status": "pending | running | completed | failed | skipped | cancelled",
  "trigger": "manual | webhook",
  "head_sha": "<sha>", "base_sha": "<sha>",
  "error_code": null, "skip_reason": null, "duration_ms": 42000,
  "summary": {},
  "owner": "<owner-id>",
  "pull_request": {"repository": "org/repo", "number": 1, "html_url": "…", "issue_key": "ABC-12"}
}
```

`404 review run not found` возвращается и для прогона другого owner'а, если используется ключ owner'а. Коды ошибок и пропусков: [TROUBLESHOOTING.md](TROUBLESHOOTING.md#коды-ошибок-и-пропусков).

## `GET /api/v1/ops/status`

Только операторский ключ. Возвращает `in_flight` (число pending/running), `queue_jobs` (глубина очереди ARQ), `stale_minutes`, `stale_runs`, `last_completed`, `last_failures` и `hints` (диагноз простыми словами, например нет квоты модели или worker не разбирает очередь).

## Webhooks

Подпишитесь на событие **Pull request**. Обрабатываемые actions: `opened`, `synchronize`, `reopened`, `ready_for_review` и `labeled` с меткой обхода size guard (она принудительно запускает новый прогон). Настройка: [GITHUB_SETUP.md](GITHUB_SETUP.md).

- `POST /api/v1/pull-request` проверяет подпись по секретам всех owner'ов, затем маршрутизирует по репозиторию. Репозиторий другого owner'а, чем владелец секрета, — `403 owner_signature_mismatch`.
- `POST /api/v1/pull-request/{owner_id}` проверяет только секрет этого owner'а.

| HTTP | Тело | Значение |
| --- | --- | --- |
| 202 | `{"status": "pending", "review_run_id": "…", "owner": "…"}` | Поставлено в очередь или переиспользован существующий прогон для этого SHA (возвращается его статус). |
| 202 | `{"status": "ok", "event": "ping"}` | Событие ping. |
| 202 | `{"status": "ok", "event": "unknown"}` | Нет заголовка `X-GitHub-Event`. |
| 202 | `{"status": "skipped", "reason": "…"}` | `ignored_event`, `ignored_action`, `ignored_label`, `draft_pr`, `fork_pr`, `repo_not_allowed`, `unknown_owner`, `owner_not_configured`, `owner_disabled`. При пропуске draft и fork статус прогресса коммита тоже публикуется как skipped. |
| 401 | `invalid signature` | `X-Hub-Signature-256` отсутствует или неверен. На URL отключённого owner'а верная подпись получает `202 owner_disabled`. |
| 403 | `owner_signature_mismatch` | Подписано секретом другого owner'а. |
| 404 | `unknown_owner` | Неизвестный owner в URL. |
| 409 | `owner_repo_conflict` | Owner из URL не заявляет этот репозиторий. |
| 503 | текст | Не настроен ни один валидный секрет webhook (для этого owner'а): эндпойнт отключён. |
| 503 | `{"code": "pr_queue_busy"}` + `Retry-After: 5` | Блокировка очереди занята; GitHub запишет неудачную доставку, её можно доставить повторно. |

Кроме метки обхода, webhook никогда не отменяет прогон для того же SHA: он переиспользует открытый или завершённый прогон (`force: false`). Новый SHA отменяет открытый прогон для предыдущего.
