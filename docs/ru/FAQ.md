<!-- section: faq -->
# FAQ

**Бот блокирует merge?**
Нет. Ревью носят рекомендательный характер. Check run прогресса никогда не сообщает `failure` из-за замечаний, только из-за ошибок бота ([PROGRESS_STATUS.md](PROGRESS_STATUS.md)).

**Он запускает код PR?**
Он его не собирает и не устанавливает. Codex работает в склонированном checkout в read-only песочнице без доступа к сети, и ему велено не исполнять код репозитория; технически это указание пока не обеспечено ([SECURITY.md](../../SECURITY.md#threat-model-and-trust-boundaries)).

**Что отправляется провайдеру модели?**
Метаданные PR, diff, прошлые замечания, необязательный текст Jira и любые файлы checkout, которые прочитает агент. Подробнее: [DATA_HANDLING.md](DATA_HANDLING.md).

**Нужен ли webhook?**
Нет. Самая простая настройка — триггер GitHub Actions, который вызывает `POST /api/v1/reviews` с API key ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#как-запускаются-ревью)). Для webhook нужен `GITHUB_WEBHOOK_SECRET`.

**GitHub App или PAT?**
Предпочтительнее App: его права задаются по репозиториям, а комментарии идут от App, а не от человека. Для первого теста подойдёт PAT ([GITHUB_SETUP.md](GITHUB_SETUP.md)).

**`REVIEW_API_KEY` или `GITHUB_TOKEN`?**
`REVIEW_API_KEY` — пароль, который вы генерируете для защиты API этого сервиса. `GITHUB_TOKEN` (или `GITHUB_PAT`) — токен GitHub, которым сервис читает и комментирует.

**API отвечает `202`, но на PR ничего не появляется.**
См. [TROUBLESHOOTING.md](TROUBLESHOOTING.md#api-отвечает-202-на-github-ничего-не-появляется). Обычные причины: worker не запущен, нет квоты модели (`CODEX_QUOTA`), пропуск (draft, fork, права автора, size guard) или не хватает прав GitHub.

**Почему тот же коммит не отревьюили заново?**
Webhooks и `force: false` переиспользуют завершённый прогон для того же SHA. Ручной запрос (по умолчанию `force: true`) запускает новый прогон.

**Может ли один деплой обслуживать несколько организаций?**
Да, с блоком `owners:` ([MULTI_OWNER.md](MULTI_OWNER.md)).

**Можно ли ревьюить публичные репозитории?**
Да. По умолчанию замечания безопасности редактируются, а раскрытие Jira ограничено ([PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md)).

**Можно ли изменить инструкции ревью?**
Да: `codex.prompt_file` (и `pr_description.prompt_file`), см. [DEPLOY.md](DEPLOY.md#configyaml).

**Какие языки поддерживаются?**
`en` и `ru` для вывода ревью (`language.summary`, `language.details`); описания PR принимают любой код языка ([CONFIGURATION.md](CONFIGURATION.md#language)).

**Он создаёт задачи в Jira?**
Нет. Он только комментирует связанную задачу и, если включено, переводит её в `rework_status` при замечаниях P0/P1.

**Большие PR?**
Выше `size_guard.hard` или `codex.max_files` (все изменённые файлы, включая пропущенные) / `max_diff_lines` прогон пропускается с `PR_TOO_LARGE_FOR_AI_REVIEW`. Метка `autoreview:force` обходит size guard ([PR_DESCRIPTION.md](PR_DESCRIPTION.md#size-guard)).

**Как удалить сохранённые данные?**
См. [DATA_HANDLING.md](DATA_HANDLING.md#хранение-и-удаление).

**Как сообщить об уязвимости?**
Приватно, через GitHub Security Advisories ([SECURITY.md](../../SECURITY.md#reporting-a-vulnerability)).
