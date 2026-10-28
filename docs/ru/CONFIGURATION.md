<!-- section: configuration-reference-config-yaml -->
# Справочник конфигурации (`config.yaml`)

`config.yaml` содержит несекретные настройки. Секреты живут в окружении (см. [ENVIRONMENT.md](ENVIRONMENT.md)); YAML только называет переменные, в которых они лежат. Начните с [`config.example.yaml`](../../config.example.yaml).

- Путь к файлу — `CONFIG_PATH` (по умолчанию `config.yaml` относительно рабочего каталога; `/app/config.yaml` в образах Compose).
- Нет файла — действуют все значения по умолчанию ниже.
- Неизвестные ключи верхнего уровня игнорируются, поэтому старый образ может прочитать новый файл. Внутри `owners.<id>` неизвестные ключи отклоняются.
- API и worker читают файл один раз при старте. **После любого изменения перезапустите оба** (`docker compose up -d --force-recreate api worker`). Файл промпта, указанный в конфиге, перечитывается при каждом ревью.
- Невалидный файл (неверный тип, значение вне диапазона, ошибка в блоке owner'а) останавливает API и worker при старте с ошибкой, называющей поле. Значения секретов никогда не печатаются.

Колонка **Owner** показывает, можно ли переопределить ключ в блоке `owners.<id>` (см. [MULTI_OWNER.md](MULTI_OWNER.md)). Переопределения частичные и глубоко сливаются со значением верхнего уровня; слитая секция проверяется так же, как глобальная.

<!-- section: github -->
## `github`

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `github.allowed_repos` | список `owner/repo` | `[]` | свой список в `owners.<id>.github.allowed_repos` | Репозитории, которые может ревьюить legacy-owner (`default`). Точные имена без учёта регистра. **Пустой список разрешает любой репозиторий** и пишет предупреждение при старте; в production задайте явный список. Репозитории вне непустого списка пропускаются с `repo_not_allowed`. |
| `github.app_id` | int | `0` | — | Для кредов не используется. Id App legacy-owner'а берётся из `GITHUB_APP_ID`. Оставлен для совместимости. |
| `github.installation_id` | int | `0` | — | Для кредов не используется. Установка legacy-owner'а берётся из `GITHUB_INSTALLATION_ID` (или находится автоматически). Оставлен для совместимости. |

Блоки GitHub owner'ов (`owners.<id>.github.*`) перечислены в разделе [Owners](#owners).

<!-- section: jira -->
## `jira`

Используется только legacy-owner'ом. Именованные owner'ы используют только свой блок `owners.<id>.jira`; ничего не наследуется.

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `jira.base_url` | string | `""` | свой блок | URL сайта Jira. Если задана `JIRA_BASE_URL`, она его переопределяет. |
| `jira.email` | string | `""` | свой блок | Email сервисного аккаунта. Если задана `JIRA_EMAIL`, она его переопределяет. |
| `jira.projects` | map `KEY: {rework_status}` | `{}` | свой блок | Разрешённые ключи проектов. Пустая map разрешает любой ключ, найденный в PR. |
| `jira.projects.<KEY>.rework_status` | string | `"In Progress"` | свой блок | Статус, в который переводится задача при замечаниях P0/P1 и включённом `features.jira_transition`. |

Jira активна, только когда заданы base URL, email и `JIRA_API_TOKEN`. Сервисному аккаунту нужны права browse, comment и transition в этих проектах. Сервис никогда не создаёт задачи.

<!-- section: slack -->
## `slack`

Используется только legacy-owner'ом; именованные owner'ы используют свой блок `owners.<id>.slack`.

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `slack.enabled` | bool | `false` | свой блок (там по умолчанию `true`) | Отправлять уведомления и дайджест. Нужны `SLACK_BOT_TOKEN` и канал. При `false` дайджест только сохраняется в базе. |
| `slack.channel` | string | `""` | свой блок | Имя или id канала. Используйте приватный канал: туда могут уходить нередактированные детали замечаний (см. [PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md)). |

<!-- section: schedule -->
## `schedule`

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `schedule.review_digest.enabled` | bool | `true` | — | Регистрировать в worker'е job дайджеста открытых PR. |
| `schedule.review_digest.cron` | string | `"0 * * * *"` | — | Когда запускается дайджест. **Используются только поля минут и часов, и только простые числа:** `"15 * * * *"` — каждый час в :15, `"0 9 * * *"` — ежедневно в 09:00. Любая другая форма поля (`*/15`, списки, диапазоны) сводится к минуте `0` / каждому часу; день, месяц и день недели игнорируются. Часовой пояс worker'а. |

Дайджест строится по каждому owner'у и только для owner'ов с `features.digest_enabled: true`.

<!-- section: features -->
## `features`

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `features.jira_comment` | bool | `true` | да | Комментировать связанную задачу Jira после ревью. |
| `features.jira_transition` | bool | `true` | да | Переводить задачу в `rework_status` при замечаниях P0/P1. |
| `features.digest_enabled` | bool | `true` | да | Включать этого owner'а в плановый дайджест. |

<!-- section: language -->
## `language`

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `language.summary` | `en` \| `ru` | `en` | да | Язык summary ревью и заголовков замечаний. |
| `language.details` | `en` \| `ru` | `en` | да | Язык сценария, доказательств, рекомендации замечаний и подписей в комментарии PR ([комментарий ревью](PROGRESS_STATUS.md#комментарий-ревью)). Запасной язык описания PR. |

Тексты Jira и Slack пока используют английские подписи.

<!-- section: pr-description -->
## `pr_description`

Необязательные черновики описания и заголовка PR, по умолчанию выключены. Поведение: [PR_DESCRIPTION.md](PR_DESCRIPTION.md).

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `pr_description.enabled` | bool | `false` | да | Готовить черновик описания после публикации ревью. |
| `pr_description.mode` | `comment` \| `fill_empty` \| `append` | `comment` | да | Отдельное sticky-предложение; заполнить пустое тело или неизменённый шаблон; или помеченный блок под текстом автора. |
| `pr_description.title_mode` | `off` \| `until_human_edit` \| `when_invalid_or_inconsistent` | `off` | да | Обновление заголовка. `always` — устаревший алиас `until_human_edit` (пишет предупреждение); `false` означает `off`. В YAML пишите `"off"` в кавычках. |
| `pr_description.check_title_relevance` | bool | `true` | да | Сравнивать заголовок с сообщениями коммитов и сообщать о расхождениях в черновике. |
| `pr_description.timeout_seconds` | int, 1–600 | `180` | да | Общий бюджет этого необязательного шага. |
| `pr_description.max_commit_chars` | int, 1–50000 | `12000` | да | Бюджет символов для заголовков коммитов в промпте. |
| `pr_description.max_commit_messages` | int, 1–250 | `250` | да | Legacy-ограничение числа читаемых сообщений коммитов. GitHub возвращает не больше 250 коммитов PR. |
| `pr_description.language` | `auto` или код BCP 47 | не задан | да | Язык черновика. Если не задан — `pr_text.language`, затем `language.details`. `auto` определяет английский/русский по заголовку человека, телу, затем коммитам. |
| `pr_description.prompt_file` | путь | `""` | да | Замена промпта, например `prompts/local/pr_description.md`. Пусто — используется [`prompts/pr_description.md`](../../prompts/pr_description.md). Отсутствующий файл проваливает шаг. |

<!-- section: pr-text -->
## `pr_text`

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `pr_text.language` | `auto` или код BCP 47 | не задан | — | Legacy-имя `pr_description.language`, используется, только если тот ключ не задан. |

<!-- section: size-guard -->
## `size_guard`

Проверяется до любого вызова модели. Подробнее: [PR_DESCRIPTION.md](PR_DESCRIPTION.md#size-guard).

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `size_guard.soft.commits` | int > 0 | `50` | да | Выше — ревью выполняется, но черновики описания/заголовка пропускаются. |
| `size_guard.soft.changed_lines` | int > 0 | `5000` | да | То же для добавленных + удалённых строк. |
| `size_guard.hard.commits` | int > 0, ≥ soft | `150` | да | Выше — прогон пропускается с одним комментарием-уведомлением (`PR_TOO_LARGE_FOR_AI_REVIEW`). |
| `size_guard.hard.changed_lines` | int > 0, ≥ soft | `20000` | да | То же для изменённых строк. |
| `size_guard.override_label` | непустая строка | `autoreview:force` | да | Добавление этой метки запускает новый прогон в обход обоих порогов. Сравнивается без учёта регистра. |

Значение ровно на пороге допускается.

<!-- section: public-repos -->
## `public_repos`

Применяется только к репозиториям, которые GitHub сообщает как публичные (неизвестная видимость считается публичной). Подробнее: [PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md).

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `public_repos.jira_disclosure` | `none` \| `key_only` \| `full` | `key_only` | да | Что вывод бота может показывать о связанной задаче Jira. |
| `public_repos.security_findings` | `redact` \| `redact_all` \| `full` | `redact` | да | Редактировать замечания безопасности (по категории и ключевым словам), все замечания или ничего. |

<!-- section: progress-status -->
## `progress_status`

Check run или commit status на head PR. Подробнее: [PROGRESS_STATUS.md](PROGRESS_STATUS.md).

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `progress_status.enabled` | bool | `true` | да | Публиковать прогресс. |
| `progress_status.mode` | `auto` \| `check_run` \| `commit_status` \| `off` | `auto` | да | `auto`: check run для GitHub App, commit status для PAT. |
| `progress_status.reaction` | bool | `false` | да | Ставить на PR реакцию `eyes`, пока worker его ревьюит. |
| `progress_status.timeout_seconds` | float, > 0 и ≤ 30 | `5` | да | Предел на один вызов GitHub. |

<!-- section: codex -->
## `codex`

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `codex.model` | string | `gpt-5.6-sol` | да | Модель, передаваемая Codex CLI. |
| `codex.reasoning_effort` | string | `medium` | да | Reasoning effort Codex. |
| `codex.api_key_env` | имя переменной | — | **только owner** | Переменная с ключом модели для этого owner'а (если задан — обязательна и в API, и в worker'е). Без него: `OWNER_<ID>_OPENAI_API_KEY`, иначе общий `OPENAI_API_KEY`. |
| `codex.prompt_file` | путь | `""` | — | Замена промпта ревью, например `prompts/local/review.md`. Пусто — используется [`prompts/review.md`](../../prompts/review.md). Относительно рабочего каталога (`/app` в Docker). Отсутствующий файл проваливает ревью. Подставляются `{{summary_language}}` и `{{details_language}}`. |
| `codex.sandbox` | string | `read-only` | - | Значение `--sandbox` Codex. Когда `allow_unsandboxed` равен `false` (по умолчанию), при старте допускается **только** `read-only`; `workspace-write` и `danger-full-access` отклоняются. |
| `codex.allow_unsandboxed` | bool | `false` | - | Только escape hatch для trusted/dev. Когда `false` (по умолчанию), review **fail closed** с `CODEX_SANDBOX`, если Linux sandbox не стартует, и `codex.sandbox` должен быть `read-only`. Не включать для публичных репозиториев. |
| `codex.approval_policy` | string | `never` | — | Политика подтверждений Codex. Оставьте `never`: в worker'е некому отвечать на запросы. |
| `codex.timeout_seconds` | int | `600` | — | Таймаут одного вызова Codex. Держите его ниже `WORKER_JOB_TIMEOUT`. |
| `codex.max_files` | int | `100` | — | Если изменённых файлов в PR больше, считая и пропущенные (lockfile'ы, бинарные и другие файлы, не отправляемые модели): модель не вызывается, PR получает комментарий `PR_TOO_LARGE_FOR_AI_REVIEW`. |
| `codex.max_diff_lines` | int | `20000` | — | Строк diff больше этого: то же, что `max_files`. |
| `codex.max_findings` | int | `20` | — | Максимум замечаний, сохраняемых из одного ревью. |
| `codex.max_file_bytes` | int | `524288` | — | Байт патча на файл, отправляемых модели; более длинные патчи обрезаются. |

Распространённые lockfile'ы, файлы `.env*` и файлы с суффиксами изображений, PDF, архивов, шрифтов или скомпилированных бинарников не попадают в контекст модели. Другие сгенерированные, минифицированные или vendored-файлы (например `.min.js` или `.map`) не распознаются и отправляются как исходный код. Полный список: [DATA_HANDLING.md](DATA_HANDLING.md#отправляется-провайдеру-модели).

<!-- section: routing -->
## `routing`

| Ключ | Тип | По умолчанию | Owner | Описание |
| --- | --- | --- | --- | --- |
| `routing.unclaimed` | `default` \| `reject` | `default` | — | Куда идёт репозиторий, который не заявил ни один owner: к owner'у по умолчанию или отклоняется как `unknown_owner`. Неизвестные ключи в `routing` отклоняются. |

<!-- section: owners -->
## `owners`

Необязателен. Без него сервис работает как один owner, `default`, собранный из окружения и секций выше. Режимы, маршрутизация и примеры: [MULTI_OWNER.md](MULTI_OWNER.md).

Id owner'ов соответствуют `^[a-z0-9][a-z0-9_-]{0,63}$`. Имена переменных в ключах `*_env` соответствуют `^[A-Z][A-Z0-9_]*$`; литеральный секрет отклоняется. `<ID>` ниже — id owner'а в верхнем регистре, `-` заменён на `_`.

| Ключ | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `owners.<id>.default` | bool | `false` | Owner для маршрутизации по умолчанию (режимы C/D или вместо `default` в режиме B). Не больше одного. |
| `owners.<id>.enabled` | bool | `true` | `false` останавливает новую работу owner'а; прогоны в очереди пропускаются как `owner_disabled`, его открытые статусы закрываются как skipped. |
| `owners.<id>.aliases` | список id | `[]` | Другие id, на которые отзывается этот owner. `aliases: [default]` наследует строки, записанные до поддержки нескольких owner'ов (только один owner и не в режиме B). |
| `owners.<id>.github.auth` | `app` \| `pat` | `app` | Тип кредов. |
| `owners.<id>.github.app_id` | int | — | Id App (только App). |
| `owners.<id>.github.app_id_env` | имя переменной | `OWNER_<ID>_GITHUB_APP_ID` | Переменная с id App (только App). |
| `owners.<id>.github.private_key_env` | имя переменной | `OWNER_<ID>_GITHUB_APP_PRIVATE_KEY` | Переменная с PEM или путём к нему (только App). |
| `owners.<id>.github.private_key_file` | путь | — | PEM-файл вместо переменной (только App). Должен существовать при старте. |
| `owners.<id>.github.installation_id` | int | `0` | Установка App; `0` — взять из webhook или найти. Ненулевой id также направляет webhooks этой установки к этому owner'у (только App). |
| `owners.<id>.github.token_env` | имя переменной | `OWNER_<ID>_GITHUB_TOKEN` | Переменная с PAT (только PAT). |
| `owners.<id>.github.webhook_secret_env` | имя переменной | `OWNER_<ID>_GITHUB_WEBHOOK_SECRET` | Секрет webhook. Отсутствует или короче 16 символов — webhooks отключены только для этого owner'а. |
| `owners.<id>.github.allowed_repos` | список | `[]` | Заявки: точное `org/repo` или `org/*`. Пустой список принимает любой репозиторий, направленный к owner'у (предупреждение при старте). Два owner'а не могут заявить одну и ту же запись. |
| `owners.<id>.api.key_env` | имя переменной | `OWNER_<ID>_REVIEW_API_KEY` | API key этого owner'а (необязателен). Должен отличаться от ключей других owner'ов и от `REVIEW_API_KEY`. |
| `owners.<id>.jira.base_url` | string | `""` | Сайт Jira. Нужны и `base_url`, и `email`, иначе Jira для owner'а выключена (предупреждение). |
| `owners.<id>.jira.email` | string | `""` | Аккаунт Jira. |
| `owners.<id>.jira.api_token_env` | имя переменной | `OWNER_<ID>_JIRA_API_TOKEN` | Токен Jira. |
| `owners.<id>.jira.projects` | map | `{}` | Как `jira.projects`. |
| `owners.<id>.slack.enabled` | bool | `true` | Slack для этого owner'а, если блок есть. |
| `owners.<id>.slack.channel` | string | `""` | Канал; обязателен при включении. |
| `owners.<id>.slack.bot_token_env` | имя переменной | `OWNER_<ID>_SLACK_BOT_TOKEN` | Токен бота Slack. |
| `owners.<id>.public_repos`, `.language`, `.features`, `.pr_description`, `.size_guard`, `.progress_status` | частичные секции | — | Переопределения секций верхнего уровня; все поля, отмеченные **да** выше. |
| `owners.<id>.codex` | `model`, `reasoning_effort`, `api_key_env` | — | Модель, effort и ключ модели для этого owner'а. Остальные поля `codex` остаются глобальными. |

Owner'ы без блока `jira` или `slack` не делают запросов в Jira и ничего не пишут в Slack. Одна и та же установка App у двух owner'ов — ошибка; один и тот же PAT у двух owner'ов — предупреждение.
