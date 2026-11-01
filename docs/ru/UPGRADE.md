<!-- section: upgrade-and-rollback -->
# Обновление и откат

Версионированных релизов пока нет; деплои следуют за `main`. Заметные изменения перечислены в [CHANGELOG.md](../../CHANGELOG.md).

<!-- section: upgrading-a-compose-deployment -->
## Обновление деплоя на Compose

1. Прочитайте раздел `Unreleased` в changelog: новые права, ключи или миграции.
2. Сделайте резервную копию тома Postgres (`postgres_data`), например `pg_dump` из контейнера `postgres`.
3. Обновите checkout (`git pull`) и пересоберите: `docker compose up -d --build`.
4. Compose сначала запускает `migrate` (`alembic upgrade head`, однократно); `api` и `worker` стартуют только после его успешного завершения. Проверьте `docker compose logs migrate`.
5. Проверьте `GET /health`, `GET /is-ready`, лог старта API (owner'ы, owner по умолчанию, предупреждения конфига) и `GET /api/v1/ops/status`.

Без Compose: остановите worker, выполните `alembic upgrade head` с новым кодом и production `DATABASE_URL`, затем запустите API и worker.

Конфигурация остаётся совместимой: у новых ключей есть значения по умолчанию, сохраняющие прежнее поведение, а неизвестные ключи верхнего уровня YAML игнорируются. После изменения `config.yaml` или `.env` перезапустите API и worker.

<!-- section: migrations -->
## Миграции

| Ревизия | Изменение | Примечания |
| --- | --- | --- |
| `001_initial` | Репозитории, pull request'ы, прогоны ревью, замечания, переходы, снимки задач, прогоны дайджеста. | |
| `002_bot_title` | Заголовки PR, принадлежащие боту, и хеш их источника. | Нужна для `pr_description.title_mode`. |
| `003_comment_authors` | Идентичности GitHub, публиковавшие sticky-комментарии. | Запустите новую версию один раз со старыми кредами перед переключением между PAT и App, чтобы старые комментарии продолжали распознаваться. |
| `004_finding_details_visibility` | Где были созданы детали замечания (private/public). | Последняя ревизия до поддержки нескольких owner'ов. |
| `005_owner_id` | `owner_id` в репозиториях, прогонах ревью, прогонах дайджеста (по умолчанию `default`). | |
| `006_comment_authors_objects` | Авторы комментариев хранятся как объекты `{login, owner_id, kind}`. | |
| `007_findings_owner_id` | `owner_id` в замечаниях, обязательный. | Для downgrade может понадобиться `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1` (ниже). |
| `008_review_run_progress` | `progress_status`, `comment_url`, `has_sensitive_content` в прогонах ревью. | NULL для существующих строк. |

<!-- section: rollback -->
## Откат

- **Только конфигурация** (например блок owner'а): восстановите прежние `config.yaml` / `.env` и пересоздайте API и worker. Прогоны в очереди для удалённого owner'а пропускаются как `owner_not_configured`, для отключённого — как `owner_disabled`; их строки остаются. Порядок, при котором закрываются и статусы прогресса, см. в [MULTI_OWNER.md](MULTI_OWNER.md#обновление-и-откат).
- **Код с той же схемой:** переключитесь на предыдущий коммит и пересоберите.
- **Код со старой схемой:** сначала сделайте downgrade **новым образом**, затем разверните старый: `docker compose run --rm migrate alembic downgrade <revision>`. Для возврата к образу до поддержки нескольких owner'ов нужен `alembic downgrade 004_finding_details_visibility`; старый образ не может работать с обновлённой схемой (после `007` `findings.owner_id` обязателен без значения по умолчанию, после `006` авторы комментариев — объекты).
- Downgrade `007_findings_owner_id` отказывается выполняться и сообщает число таких замечаний, пока замечания нескольких owner'ов делят stable id в одном pull request. С `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1` он оставляет строку owner'а репозитория (или самую старую) и удаляет остальные вместе с их переходами.

Перед downgrade всегда делайте резервную копию тома Postgres.

<!-- section: moving-to-several-owners -->
## Переход на несколько owner'ов

Пошаговое обновление до блока `owners:`, сохранение истории через `aliases: [default]` и откат: [MULTI_OWNER.md](MULTI_OWNER.md#обновление-и-откат).

<!-- section: m3-container-proxy-rolling -->
## Container backend + model-proxy (M3): без rolling mix

Worker, launcher и model-proxy для `codex.runner_backend: container` **не** совместимы
по частям на срезе M3 proxy-token. Обновляйте вместе:

1. **Drain:** остановите intake (пауза GitHub Actions / webhook, или scale worker в 0 после
   завершения текущих ARQ jobs). Дождитесь, пока не останется активных review-контейнеров /
   launcher jobs (`docker compose ps`, логи launcher/reaper).
2. **Upgrade вместе:** rebuild и recreate **worker + launcher + model-proxy** из одного
   checkout (`docker compose -f docker-compose.yml -f docker-compose.container-runner.yml up -d --build`
   или `./scripts/stack.ps1 up -Container`). Перед стартом создайте/обновите `model-proxy.env` из
   `model-proxy.env.example`; реальные ключи не должны попадать в env worker/launcher.
3. **Rollback:** симметрично — recreate тех же трёх сервисов с предыдущего commit (и парного
   `model-proxy.env`). Не запускайте новый worker против старого launcher (и наоборот):
   launcher отвечает **426** `CODEX_UPGRADE_REQUIRED` с текстом
   `worker too old: upgrade worker, launcher and model-proxy together`, если нет `proxy_token`
   или прислан legacy `X-Model-Api-Key` (ключ никогда не форвардится).

<!-- section: worker-metrics-port-no-longer-published -->
## Порт метрик worker'а больше не публикуется

Compose-файл больше не пробрасывает порт метрик worker'а на хост (`127.0.0.1:9100`); он открыт только в Docker-сети, поэтому `docker compose up --scale worker=N` работает. Если ваш Prometheus скрейпил `127.0.0.1:9100` / `localhost:9100`, переведите его на Docker-сеть: подключите Prometheus к сети стека и используйте поставляемый job с `dns_sd_configs` из [`observability/prometheus-scrape.yml`](../../observability/prometheus-scrape.yml), который скрейпит каждую реплику. Чтобы сохранить порт на хосте для одного worker'а, добавьте `docker-compose.override.yml` с `ports: ["127.0.0.1:9100:9100"]` для `worker` (тогда его нельзя масштабировать). Перезагрузите и `observability/alerts.yml`: `OpenPrReviewWorkerDown` сохраняет `severity: page`, но теперь срабатывает, только когда не найдено ни одной реплики worker'а или все известные цели worker'а недоступны. Случай отдельной реплики (найденную реплику не удаётся скрейпить) перенесён в новый `OpenPrReviewWorkerTargetDown` с `severity: warning`; добавьте для него маршрут, если хотите получать эти уведомления.

<!-- section: new-github-permissions -->
## Новые права GitHub

Статус прогресса (начиная с `008_review_run_progress`) использует **Checks: Read and write** (App) или **Commit statuses: Read and write** (PAT). Без них ревью продолжают работать без статуса ([PROGRESS_STATUS.md](PROGRESS_STATUS.md)).
