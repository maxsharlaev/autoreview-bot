<!-- section: multiple-owners -->
# Несколько owner'ов

Один деплой может обслуживать несколько owner'ов GitHub (организаций или аккаунтов), у каждого свои креды GitHub, секрет webhook, API key, Jira, Slack, политики и ключ модели. Конфиг без `owners:` работает точно так же, как раньше. Как это устроено внутри: [ARCHITECTURE.md](ARCHITECTURE.md#несколько-ownerов).

<!-- section: modes -->
## Режимы

«Legacy-креды GitHub» — это `GITHUB_TOKEN`/`GITHUB_PAT` или пара `GITHUB_APP_ID` и `GITHUB_APP_PRIVATE_KEY`, заданные в окружении.

| Режим | Как определяется | Owner'ы | Owner по умолчанию |
| --- | --- | --- | --- |
| A. Legacy | нет блока `owners:` | `default`, собранный из текущего env и секций верхнего уровня `github`/`jira`/`slack` | `default` |
| B. Legacy + дополнительные owner'ы | legacy-креды GitHub в env **и** блок `owners:` | `default` плюс перечисленные owner'ы | `default`, если ни у одного owner'а нет `default: true` |
| C. Один блок owner'а | нет legacy-кредов GitHub, один owner | этот owner | этот owner |
| D. Несколько owner'ов | нет legacy-кредов GitHub, два или больше owner'ов | перечисленные owner'ы | тот, у кого `default: true`, иначе первый (предупреждение при старте) |

Два owner'а с `default: true`, owner с именем `default` в режиме B, один и тот же репозиторий или `org/*`, заявленный двумя owner'ами, одна и та же установка App или API key у двух owner'ов, API key owner'а, равный `REVIEW_API_KEY`, или отсутствующий обязательный секрет останавливают старт с ошибкой, которая называет переменную, но никогда не её значение.

<!-- section: routing -->
## Маршрутизация

Запрос направляется: явно указанному owner'у (`/api/v1/pull-request/<id>`, заголовок `X-Review-Owner`, `?owner=` или API key owner'а); иначе owner'у, у которого точное `org/repo` есть в `github.allowed_repos`; иначе owner'у с `org/*`; иначе owner'у, чей ненулевой `installation_id` совпадает с webhook; иначе `routing.unclaimed: default` (owner по умолчанию) или `reject` (`unknown_owner`). Затем применяется allowlist выбранного owner'а. Явный owner для репозитория, заявленного другим owner'ом, получает 409 `owner_repo_conflict`; webhook, подписанный секретом одного owner'а для репозитория другого, получает 403 `owner_signature_mismatch`. `REVIEW_API_KEY` — операторский ключ для всех owner'ов; `owners.<id>.api.key_env` — ключ одного owner'а, который видит только свои прогоны.

<!-- section: secrets -->
## Секреты

YAML содержит только имена переменных (`*_env`) или пути к файлам ключей; литеральный секрет отклоняется. Без явного `*_env` имя — `OWNER_<ID>_<NAME>` (`<ID>` в верхнем регистре, `-` → `_`): `GITHUB_TOKEN`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, `REVIEW_API_KEY`, `JIRA_API_TOKEN`, `SLACK_BOT_TOKEN`, `OPENAI_API_KEY`; например `OWNER_EXAMPLE_ORG_GITHUB_TOKEN`. `DATABASE_URL`, `REDIS_URL`, `OPENAI_API_KEY` (общий ключ модели по умолчанию) и настройки worker'а/логов/метрик остаются общими. Невалидный секрет webhook отключает webhooks только для этого owner'а.

Все имена секретов: [ENVIRONMENT.md](ENVIRONMENT.md#переменные-ownerов-owner_id_). Все ключи owner'а: [CONFIGURATION.md](CONFIGURATION.md#owners).

<!-- section: example-two-organizations-with-pats -->
## Пример: две организации с PAT

```yaml
routing:
  unclaimed: reject          # repositories of any other account are refused
owners:
  org-a:
    default: true
    github:
      auth: pat              # token from OWNER_ORG_A_GITHUB_TOKEN
      allowed_repos: ["org-a/*"]
  org-b:
    github:
      auth: pat              # token from OWNER_ORG_B_GITHUB_TOKEN
      allowed_repos: ["org-b/*"]
```

Fine-grained PAT принадлежит одному владельцу ресурсов (пользователю или одной организации), поэтому каждому owner'у нужен свой токен. Оставьте legacy `GITHUB_TOKEN` пустым, чтобы остаться в режиме D.

<!-- section: connecting-another-organization -->
## Подключение ещё одной организации

Два способа подключить ещё одну организацию:

1. **Тот же GitHub App, другая установка.** Установите App в другой организации. Дайте owner'у те же имена переменных App (и секрет webhook) и заявите его репозитории через `allowed_repos: [org-b/*]` или задайте его `installation_id`. События по-прежнему идут на `/api/v1/pull-request`; сервис маршрутизирует их по репозиторию и id установки. Общий секрет здесь допустим.
2. **Свой App или PAT.** Создайте отдельный App или PAT для owner'а и направьте его webhook на `https://review.example.com/api/v1/pull-request/<owner-id>`. Этот URL проверяет только секрет этого owner'а.

Для триггеров Actions используйте операторский `REVIEW_API_KEY` с `X-Review-Owner: <owner-id>` (или `?owner=`) либо дайте репозиторию ключ owner'а из `api.key_env`. Дополнительные ответы:

| Ответ | Значение |
| --- | --- |
| `403 owner_signature_mismatch` | Webhook подписан секретом другого owner'а, чем тот, к которому маршрутизируется репозиторий |
| `202 skipped unknown_owner` / API `403` | Ни один owner не заявляет репозиторий, и `routing.unclaimed: reject` |
| `404 unknown_owner` | Неизвестный owner в URL, `X-Review-Owner` или `?owner=` |
| `409 owner_repo_conflict` | Явный owner указан для репозитория, заявленного другим owner'ом |
| `403 owner_forbidden` | API key owner'а использован для другого owner'а |
| `401` на `/pull-request/<id>` отключённого owner'а | Нет подписи или она неверна; верная подпись получает `202 owner_disabled` |

<!-- section: keeping-pre-multi-owner-history -->
## Сохранение истории до поддержки нескольких owner'ов

Замечания, прогоны ревью и записи об авторах комментариев, созданные до появления блока `owners:`, хранятся под legacy id owner'а `default`. Они привязываются явно, никогда не через флаг `default: true`:

- только legacy-креды в env или legacy env плюс блок `owners:`: их сохраняет legacy-owner `default`;
- только блок `owners:`: ими владеет owner, у которого `default` указан в `aliases` (только один owner);
- иначе их никто не видит: прошлые замечания, прошлый head SHA, блокеры дайджеста и доверенные авторы комментариев из этой истории игнорируются, а API/worker пишут предупреждение при старте.

При переходе с legacy-кредов в env только на именованных owner'ов добавьте алиас owner'у, который должен унаследовать эту историю:

```yaml
owners:
  example-org:
    default: true          # routing fallback only; moving it does not move history
    aliases: [default]     # inherits rows recorded as owner 'default'
    github:
      auth: pat
```


<!-- section: jira-and-slack-per-owner -->
## Jira и Slack по owner'ам

Именованный owner использует только свои блоки `jira` и `slack`; от секций верхнего уровня или legacy-owner'а `default` ничего не наследуется. Без блока `jira` owner не делает запросов в Jira; без блока `slack` он ничего не публикует, включая ежечасный дайджест. Чтение, комментарии и переходы Jira используют сайт, аккаунт и `projects` owner'а (разрешённые ключи проектов и их `rework_status`). Каждый owner со Slack получает свой дайджест в свой канал. Секреты — имена переменных: `api_token_env` / `bot_token_env` или по умолчанию `OWNER_<ID>_JIRA_API_TOKEN` / `OWNER_<ID>_SLACK_BOT_TOKEN`. Чтобы несколько owner'ов использовали один аккаунт Jira или бота Slack, укажите им одно и то же имя переменной.

```yaml
owners:
  example-org:
    github: { auth: pat, allowed_repos: [example-org/*] }
    jira:
      base_url: https://example-org.atlassian.net
      email: review-bot@example-org.example
      api_token_env: OWNER_EXAMPLE_ORG_JIRA_API_TOKEN
      projects:
        ABC: { rework_status: "In Progress" }
    slack:
      enabled: true
      channel: "#example-org-review"
      bot_token_env: OWNER_EXAMPLE_ORG_SLACK_BOT_TOKEN
```

URL webhook отключённого owner'а `/api/v1/pull-request/<id>` отвечает `owner_disabled` только на запросы, подписанные секретом webhook этого owner'а; остальные получают 401.


<!-- section: policies-and-model-per-owner -->
## Политики и модель по owner'ам

`public_repos`, `language`, `features` (`jira_comment`, `jira_transition`, `digest_enabled`), `pr_description`, `size_guard` и `progress_status` можно переопределить для owner'а. Переопределения частичные и глубоко сливаются с секциями верхнего уровня: незаданные поля сохраняют глобальное значение, а слитая секция проверяется так же, как глобальная (например, `size_guard.hard` должен оставаться не меньше `size_guard.soft`). Они применяются ко всему, что их читает для прогонов этого owner'а: промпт и комментарий ревью, редактирование для публичных репозиториев, size guard и его `override_label` (в том числе в проверке метки webhook), черновики описания PR, комментарии/переходы Jira и дайджест. Расписание дайджеста остаётся глобальным; `features.digest_enabled` решает по каждому owner'у.

`codex.model` и `codex.reasoning_effort` можно задать для owner'а; остальные поля `codex` (песочница, политика подтверждений, лимиты, файл промпта) остаются глобальными. Ключ модели для прогонов owner'а — `codex.api_key_env` (если задан — обязателен), иначе `OWNER_<ID>_OPENAI_API_KEY`, если есть, иначе общий `OPENAI_API_KEY`. В песочницу Codex попадает только этот ключ и только для прогонов этого owner'а; ключи других owner'ов и секреты owner'ов никогда не попадают в окружение Codex или git.

```yaml
owners:
  example-org:
    github: { auth: pat, allowed_repos: [example-org/*] }
    public_repos: { security_findings: redact_all }
    language: { summary: ru, details: ru }
    features: { jira_transition: false, digest_enabled: false }
    size_guard:
      hard: { commits: 100 }
      override_label: example:force-review
    codex:
      model: gpt-5.6-sol
      reasoning_effort: high
      api_key_env: OWNER_EXAMPLE_ORG_MODEL_KEY
```


<!-- section: upgrade-and-rollback -->
## Обновление и откат

1. Разверните новый образ и выполните `alembic upgrade head` (Compose делает это в `migrate`), не трогая конфиг: поведение не меняется, логи/метрики показывают `owner=default`.
2. Добавьте `owners.<id>` и его env `OWNER_<ID>_*`, затем перезапустите API и worker; лог старта API показывает число owner'ов, owner'а по умолчанию и предупреждения конфига owner'ов.
3. Подключите события нового owner'а: установите тот же GitHub App в другой организации (те же имена переменных, `allowed_repos: [org-b/*]` или `installation_id`) или дайте owner'у свой App/PAT с webhook на `/api/v1/pull-request/<id>`.
4. Проверьте ручной `POST /api/v1/reviews` с `X-Review-Owner` и без него, тестовый PR в каждой организации и `owner` в `GET /api/v1/reviews/{id}`.
5. Необязательно: перенесите legacy-настройку в именованного owner'а с `default: true`, `aliases: [default]` и `*_env`, указывающими на существующие имена переменных (см. выше).
6. Откат в пределах версий с несколькими owner'ами: удалите или отключите блок `owners.<id>` и перезапустите; прогоны в очереди для этого owner'а пропускаются с `owner_not_configured` / `owner_disabled`, его строки остаются в базе. Открытые статусы прогресса отключённого owner'а закрываются как `skipped`; статусы удалённого закрыть нельзя (см. [PROGRESS_STATUS.md](PROGRESS_STATUS.md#owner-отключён-или-удалён)).
7. Откат к образу до поддержки нескольких owner'ов: новым образом сначала выполните `alembic downgrade 004_finding_details_visibility`, затем разверните старый образ. Старый образ не может работать с обновлённой схемой как есть: после `007_findings_owner_id` `findings.owner_id` обязателен без значения по умолчанию, а после `006_comment_authors_objects` авторы комментариев хранятся как объекты. Если замечания нескольких owner'ов делят stable id в одном pull request, downgrade `007` отказывается выполняться и сообщает их число; задайте `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1`, чтобы оставить строку owner'а репозитория (или самую старую) и удалить остальные вместе с их переходами.
