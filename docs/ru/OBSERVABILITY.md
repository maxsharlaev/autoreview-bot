# Наблюдаемость: скрейпинг наших эндпойнтов *вашим* Prometheus / Grafana

Этот сервис не запускает Grafana, Prometheus или Loki. Он только **экспортирует**:

| Экспорт | Где | Аутентификация |
| --- | --- | --- |
| Текст Prometheus | API `GET /metrics` (`:8000`) | нет |
| Текст Prometheus | Worker `GET /metrics` (`:9100`) | нет |
| Снимок ops | `GET /api/v1/ops/status` | Bearer `REVIEW_API_KEY` |
| Логи | stdout (`LOG_FORMAT=text\|json`) | — |

В `.env` нет URL наблюдаемости. Скрейпер платформы подключается сам.

## Prometheus (существующий)

Направьте scrape job'ы на API и worker. Не публикуйте `/metrics` в интернет.

Влейте [observability/prometheus-scrape.yml](../../observability/prometheus-scrape.yml) в конфиг вашего Prometheus (или Alloy). Имена хостов:

- Та же Docker-сеть: `api:8000`, `worker:9100`
- Опубликованные порты хоста: `localhost:8000`, `localhost:9100` (или IP виртуальной машины)
- Kubernetes: scrape Service/Pod / `PodMonitor` / `ServiceMonitor` на этих портах

Загрузите [observability/alerts.yml](../../observability/alerts.yml) как rule file (`OpenPrReviewWorkerDown`, `OpenPrReviewCodexQuota`, `OpenPrReviewQueueNotDraining`, `OpenPrReviewStaleReviews`).

## Grafana (существующая)

1. Datasource Prometheus → тот Prometheus, который скрейпит Open PR Review. Необязательно Loki для логов.
2. **Dashboards → Import** → [observability/grafana/dashboards/autoreview-bot.json](../../observability/grafana/dashboards/autoreview-bot.json).
3. Дашборд ожидает UID datasource'ов `prometheus` и `loki`. Переназначьте, если у вас другие.

## Loki (существующий)

Отправляйте stdout контейнеров **вашим** агентом. Compose задаёт `OPEN_PR_REVIEW_SERVICE=api|worker`. Задавайте `LOG_FORMAT=json`, только если ваш конвейер ждёт JSON; значение по умолчанию `text` подходит для `docker compose logs`.

## Env (приложения, не стека метрик)

| Переменная | По умолчанию | Роль |
| --- | --- | --- |
| `LOG_FORMAT` | `text` | `json`, если ваш агент логов ждёт JSON |
| `OPEN_PR_REVIEW_SERVICE` | `unknown` | поле лога `service` (Compose уже задаёт) |
| `METRICS_PORT` | `9100` | порт worker'а для `/metrics` |
| `LOG_LEVEL` | `INFO` | |

## Метрики, важные для worker'ов

| Метрика | Значение |
| --- | --- |
| `up{job="open-pr-review-worker"}` | Процесс worker'а скрейпится |
| `open_pr_review_worker_jobs_in_progress` | Job на этом процессе сейчас |
| `open_pr_review_arq_queue_jobs` | Очередь ожидания ARQ |
| `open_pr_review_review_in_flight{status="pending\|running"}` | Незавершённые `review_runs` |
| `open_pr_review_review_stale_in_flight` | pending/running старше 15 мин |
| `open_pr_review_review_runs_total{status,error_code,trigger}` | Завершённые прогоны |
| `open_pr_review_review_recent_failures{error_code}` | Ошибки за последний час |
| `open_pr_review_codex_attempts_total{result}` | `ok`, `CODEX_QUOTA`, `CODEX_AUTH`, … |
| `open_pr_review_review_enqueue_total{result}` | `queued` или `reused` |

`CODEX_QUOTA` при повторных попытках PR — это закончившиеся кредиты OpenAI: API по-прежнему отвечает 202, worker быстро падает. Это не зависание.

Шаги дежурного: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
