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

<!-- section: dev-prometheus-and-grafana-optional -->
## Prometheus, Grafana и Loki для разработки (необязательно)

Только для локальных проверок: [docker-compose.observability.yml](../../docker-compose.observability.yml) добавляет сервисы `prometheus`, `grafana`, `loki`, `alloy` и `docker-socket-proxy` в Docker-сеть стека. Не используйте его в продакшене. Alertmanager нет, данные хранятся в `tmpfs` и пропадают при остановке контейнера. Prometheus подключён и к `default`, и к `internal`, потому что api/worker только в `internal`.


```bash
# Full stack with Prometheus, Grafana and Loki
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --build
# Stop everything (Postgres data is kept)
docker compose -f docker-compose.yml -f docker-compose.observability.yml down
```

<!-- section: dev-prometheus -->
### Prometheus

[observability/dev/prometheus.yml](../../observability/dev/prometheus.yml) подключает те же scrape jobs (`prometheus-scrape.yml`) и правила алертов (`alerts.yml`), что и выше. UI доступен только на `http://127.0.0.1:9090`: цели на `/targets`, алерты на `/alerts`. После правки `alerts.yml` перезапустите контейнер, чтобы перечитать правила. CI запускает `promtool check config` на этом собранном конфиге (тот же образ и те же mounts, что в overlay) и `promtool check rules` на `alerts.yml`.

<!-- section: dev-grafana -->
### Grafana

Grafana доступна только на `http://127.0.0.1:3000`. Она создаёт datasource `Prometheus` (uid `prometheus`, `http://prometheus:9090`) из [observability/dev/grafana/provisioning](../../observability/dev/grafana/provisioning) и загружает поставляемый дашборд [observability/grafana/dashboards/autoreview-bot.json](../../observability/grafana/dashboards/autoreview-bot.json) как домашнюю страницу. Поставляемый дашборд доступен только для чтения, анонимные пользователи могут его смотреть. Вход как `admin` с паролем из `GRAFANA_ADMIN_PASSWORD` (по умолчанию `admin`, только локально) позволяет через **Save as** сделать копию для экспериментов; копия живёт до перезапуска контейнера. Постоянные изменения вносите в `observability/grafana/dashboards/autoreview-bot.json`. Также создаётся datasource `Loki` (uid `loki`, `http://loki:3100`) для панели логов дашборда.

<!-- section: dev-loki-and-alloy -->
### Loki и Alloy

Loki ([observability/dev/loki.yml](../../observability/dev/loki.yml)) работает одним бинарником без порта на хосте; Grafana обращается к нему по `http://loki:3100`. Хранилище — tmpfs на 512 МБ, компактор держит ретеншн 24 часа. Alloy ([observability/dev/alloy.alloy](../../observability/dev/alloy.alloy)) читает stdout и stderr контейнеров этого Compose-проекта через закреплённый `wollomatic/socket-proxy` во внутренней сети (без портов на хосте) и отправляет их в Loki с метками `service` (имя Compose-сервиса, например `worker`, `api`) и `container`. Пример для Explore: `{service="worker"}`.

В логах ровно то, что показывает `docker compose logs`, и ничего больше, поэтому обращайтесь с ними так же: только для разработки, не открывайте Loki и Grafana дальше `127.0.0.1`. Монтирование Docker socket с `:ro` — не граница API: socket монтирует только proxy. Allowlist proxy разрешает только GET list/inspect/logs и networks (плюс ping/version); archive, export, create и exec запрещены. Alloy фильтрует по этому Compose-проекту (имя, которое Compose выводит из каталога или `-p` / `COMPOSE_PROJECT_NAME`). При первом запуске Loki отклоняет строки старше 7 дней от давно работающих контейнеров; это видно как ошибки `timestamp too old` в его логе и не страшно.

> **Предупреждение (принятый риск):** фильтр Compose-проекта живёт только в конфиге Alloy. Скомпрометированный Alloy через proxy может вызывать inspect и logs для **всех** контейнеров на хосте, включая переменные окружения и секреты посторонних контейнеров. Не запускайте этот logs overlay на машине с чувствительными контейнерами вне этого проекта. Только для разработки.

<!-- section: dev-prometheus-only-and-scaling -->
### Только Prometheus и масштабирование

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d prometheus
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --scale worker=2 worker
docker compose -f docker-compose.yml -f docker-compose.observability.yml rm -sf prometheus grafana loki alloy docker-socket-proxy
```

Первая команда запускает только Prometheus, вторая масштабирует worker'ы (все реплики видны в `open-pr-review-worker`), последняя удаляет сервисы наблюдаемости.

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

`open_pr_review_arq_queue_jobs`, `open_pr_review_review_in_flight`, `open_pr_review_review_stale_in_flight` и `open_pr_review_review_recent_failures` — глобальные (общий Redis / Postgres). API и каждый worker экспортируют одни и те же значения, поэтому поставляемый дашборд и алерты оборачивают их в `max(...)` (или `max by (error_code)`), чтобы показывать одно число и срабатывать один раз.

`CODEX_QUOTA` при повторных попытках PR — это закончившиеся кредиты OpenAI: API по-прежнему отвечает 202, worker быстро падает. Это не зависание.

Шаги дежурного: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
