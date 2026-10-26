<!-- section: observability-scrape-our-endpoints-from-your-prometheus-grafana -->
# Наблюдаемость: скрейпинг наших эндпойнтов *вашим* Prometheus / Grafana

Этот сервис не запускает Grafana, Prometheus или Loki. Он только **экспортирует**:

| Экспорт | Где | Аутентификация |
| --- | --- | --- |
| Текст Prometheus | API `GET /metrics` (`:8000`) | нет |
| Текст Prometheus | Worker `GET /metrics` (`:9100`) | нет |
| Снимок ops | `GET /api/v1/ops/status` | Bearer `REVIEW_API_KEY` |
| Логи | stdout (`LOG_FORMAT=text\|json`) | — |

В `.env` нет URL наблюдаемости. Скрейпер платформы подключается сам.

<!-- section: prometheus-existing -->
## Prometheus (существующий)

Направьте scrape job'ы на API и worker. Не публикуйте `/metrics` в интернет.

Влейте [observability/prometheus-scrape.yml](../../observability/prometheus-scrape.yml) в конфиг вашего Prometheus (или Alloy). Имена хостов:

- Та же Docker-сеть: `api:8000`, `worker:9100`
- Опубликованные порты хоста: `localhost:8000` только для API. `:9100` worker'а открыт только в Docker-сети, поэтому Prometheus должен подключиться к этой сети (или работать в ней). Поставляемый job worker'а использует `dns_sd_configs` по имени сервиса `worker`, который возвращает по адресу на каждую реплику, так что скрейпятся все масштабированные worker'ы (Swarm: `tasks.worker`). Статическая цель `worker:9100` скрейпила бы только одну реплику.
- Kubernetes: scrape Service/Pod / `PodMonitor` / `ServiceMonitor` на этих портах

Загрузите [observability/alerts.yml](../../observability/alerts.yml) как rule file (`OpenPrReviewWorkerDown`, `OpenPrReviewWorkerTargetDown`, `OpenPrReviewApiDown`, `OpenPrReviewCodexQuota`, `OpenPrReviewQueueNotDraining`, `OpenPrReviewStaleReviews`). `OpenPrReviewWorkerDown` (`page`) срабатывает, когда не найдено ни одного worker'а (`absent(up{job="open-pr-review-worker"})`) или все известные цели worker'а недоступны (`max(up{job="open-pr-review-worker"}) == 0`): ревью копятся в очереди и не выполняются. Второй случай — обычный полный отказ: когда все worker'ы остановлены, имя `worker` перестаёт резолвиться, и Prometheus сохраняет последние найденные цели с `up == 0`. `OpenPrReviewWorkerTargetDown` (`warning`) срабатывает, когда найденную реплику worker'а не удаётся скрейпить (`up == 0`); остальные реплики могут продолжать обрабатывать ревью. При полном отказе он срабатывает вместе с `OpenPrReviewWorkerDown`. Ограничение: при нескольких репликах остановленная реплика пропадает из DNS-обнаружения Docker вместо того, чтобы сообщить `up == 0`, поэтому потеря части (не всех) реплик не вызывает ни одного из этих алертов. Отслеживайте частичную потерю метриками Docker или оркестратора (желаемое и запущенное число реплик); алерт на рост очереди запланирован как следующий шаг.

<!-- section: dev-prometheus-optional -->
## Prometheus для разработки (необязательно)

Только для локальных проверок: [docker-compose.observability.yml](../../docker-compose.observability.yml) добавляет сервис `prometheus` в Docker-сеть стека. Он загружает [observability/dev/prometheus.yml](../../observability/dev/prometheus.yml), который подключает те же scrape jobs (`prometheus-scrape.yml`) и правила алертов (`alerts.yml`), что и выше. UI опубликован только на `127.0.0.1:9090`: цели на `/targets`, алерты на `/alerts`. Alertmanager нет, данные хранятся в `tmpfs` и пропадают при остановке контейнера. Не используйте его в продакшене.

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d prometheus
docker compose up -d --scale worker=2
docker compose -f docker-compose.yml -f docker-compose.observability.yml rm -sf prometheus
```

Первая команда запускает Prometheus, вторая масштабирует worker'ы (все реплики видны в `open-pr-review-worker`), последняя удаляет Prometheus. После правки `alerts.yml` перезапустите контейнер, чтобы перечитать правила.

<!-- section: grafana-existing -->
## Grafana (существующая)

1. Datasource Prometheus → тот Prometheus, который скрейпит Open PR Review. Необязательно Loki для логов.
2. **Dashboards → Import** → [observability/grafana/dashboards/autoreview-bot.json](../../observability/grafana/dashboards/autoreview-bot.json).
3. Дашборд ожидает UID datasource'ов `prometheus` и `loki`. Переназначьте, если у вас другие.

<!-- section: loki-existing -->
## Loki (существующий)

Отправляйте stdout контейнеров **вашим** агентом. Compose задаёт `OPEN_PR_REVIEW_SERVICE=api|worker`. Задавайте `LOG_FORMAT=json`, только если ваш конвейер ждёт JSON; значение по умолчанию `text` подходит для `docker compose logs`.

<!-- section: env-app-not-the-metrics-stack -->
## Env (приложения, не стека метрик)

| Переменная | По умолчанию | Роль |
| --- | --- | --- |
| `LOG_FORMAT` | `text` | `json`, если ваш агент логов ждёт JSON |
| `OPEN_PR_REVIEW_SERVICE` | `unknown` | поле лога `service` (Compose уже задаёт) |
| `METRICS_PORT` | `9100` | порт worker'а для `/metrics` |
| `LOG_LEVEL` | `INFO` | |

<!-- section: metrics-that-matter-for-workers -->
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
