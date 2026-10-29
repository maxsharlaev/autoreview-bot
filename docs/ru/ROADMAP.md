<!-- section: roadmap-autoreview-bot -->
# Роадмап: autoreview-bot

Живой план self-hosted бота ревью PR. Английский роадмап канонический; см. [английскую версию](../ROADMAP.md).

Статусы запланированных задач: **planned** / **design** / **in progress** / **testing** / **done**.

<!-- section: current-state -->
## 1. Текущее состояние

Что работает сегодня на `main` (и чего нет).

<!-- section: works-today -->
### Что уже есть

| Area | Status |
| --- | --- |
| Review pipeline | GitHub PR -> ARQ worker -> Codex CLI -> JSON schema -> sticky comment; опциональный progress check run / commit status |
| Concurrency | Per-PR advisory lock, deterministic job ids, conditional run claim, `HEAD_CHANGED` at pickup; для private repos повторная проверка head перед publish |
| Multi-owner | Несколько GitHub owners в одном деплое: routing, per-owner GitHub / webhook / API / Jira / Slack / policies / model key ([MULTI_OWNER.md](MULTI_OWNER.md), #9 done) |
| Public repos | Редактура security findings и контроль disclosure для Jira ([PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md)) |
| Sandbox / agent isolation | Fail-closed Linux sandbox; `shell_environment_policy` + execpolicy denylist; ключ модели не наследуется дочерними процессами агента; AR-001/AR-002 — принятые platform limits ([SECURITY.md](../../SECURITY.md)) |
| ReviewExecutor | **M1 done** на `main` ([#26](https://github.com/maxsharlaev/autoreview-bot/pull/26)): `codex.runner_backend` с default `in_process`; `container` отклоняется при загрузке конфига, пока не приземлится M2. **M2 testing** в [#27](https://github.com/maxsharlaev/autoreview-bot/pull/27) (ещё не в `main`): opt-in эфемерные review-контейнеры через dedicated launcher |
| Observability | Prometheus-метрики API/worker, alert rules, Grafana dashboard; opt-in Compose overlay для Prometheus, Grafana, Loki и Alloy ([OBSERVABILITY.md](OBSERVABILITY.md)) |
| PR description (basic) | Опциональная пост-ревью правка title/body (`comment` / `fill_empty` / `append`); политики rewrite low-quality ещё нет |
| Docs / ops | Зеркало docs EN+RU; Compose overlays для observability |

<!-- section: honest-limitations -->
### Честные ограничения

- **Default по-прежнему `in_process`.** Container backend **нет на `main`**; M2 тестируется в #27. Пока M2 не смержен, доступен только `in_process`. После M2 ключ модели всё ещё попадает в review-процесс до M3.
- **Остаются platform gaps Codex** для public repos: нет default-deny внутри read-only sandbox; execpolicy только forbid-only; unmatched in-sandbox tools всё ещё могут читать checkout.
- **Пустой `github.allowed_repos` принимает все доступные репозитории** (только startup warning) - deny-by-default не сделан.
- **Recheck head перед publish** надёжен для private repos; для public publish тот же гарантийный путь ещё в gap.
- **Нет admin UI**; ops опираются на status API, логи и Grafana.
- **Записи в Jira** (comment / P0-P1 transition) всё ещё могут идти от model severity без human gate.
- **Один code host и один reviewer CLI** (GitHub + Codex); второго host/runner нет.
- **Пробелы в тестах:** Redis/ARQ и external contract tests, более полный e2e publish failure / queue recovery; локальные pre-commit hooks не стандартизированы.
- **Контакт для security-репортов:** только GitHub private advisories; альтернативный e-mail всё ещё TODO в `SECURITY.md`.

<!-- section: planned-work -->
## 2. Запланированные задачи

<!-- section: container-strict-review-path -->
### Container / strict review path

| Item | Status | Notes |
| --- | --- | --- |
| **M1 - ReviewExecutor + `in_process`** | **done** | [#26](https://github.com/maxsharlaev/autoreview-bot/pull/26) на `main` |
| **M2 - Ephemeral review container + launcher** | **testing** | [#27](https://github.com/maxsharlaev/autoreview-bot/pull/27) (ещё не на `main`): launcher-only Docker socket, hard-coded create spec, fail closed |
| **M3 - Model-API egress proxy + strict egress allowlist** | **design** | Убрать ключ модели из review-контейнера |
| **M4 - Container backend as default** | **planned** | После M3: сделать `runner_backend=container` default |
| **Linux-host AppArmor for review containers** | **planned** | Docker Desktop: N/A; на Linux-хостах — dedicated profile вместо default/unconfined follow-up |

<!-- section: product-ux -->
### Product / UX

| Item | Status | Notes |
| --- | --- | --- |
| **Admin panel (runs, queue, finding management)** | **planned** | Optional (`ADMIN_UI_ENABLED=false`); queue/runs, filters, safe retry, audit log; управление findings / cleanup sensitive-comment (`has_sensitive_content`); authz отдельно от public API |
| **PR description / title policy + rewrite** | **planned** | [#20](https://github.com/maxsharlaev/autoreview-bot/issues/20): `pr_description.policy` / `title_policy`, per-repo overrides, deterministic low-quality checks, rewrite с сохранением текста автора |
| **Two-pass review (spec then verify)** | **planned** | Часть #20 / spec-driven направления: pass 1 пишет spec; pass 2 в новой сессии сверяет код со spec (и Jira, если есть); отдельно code defects и inconsistencies |
| **Human approval for model-driven Jira actions** | **planned** | Предлагать P0/P1 transition/comment; подтверждение через label / reaction / admin panel |

<!-- section: reliability-security-observability -->
### Reliability / security / observability

| Item | Status | Notes |
| --- | --- | --- |
| **Deny-by-default empty allowlist** | **planned** | Отклонять пустой `github.allowed_repos` (или эквивалент) вместо warn-and-allow |
| **Head recheck before publish (all repos)** | **planned** | Расширить гарантию private-repo на public publish path |
| **Stronger model-output verification** | **planned** | Отклонять wrong SHA, paths вне diff, impossible lines, inconsistent previous-finding status; явно помечать incomplete coverage |
| **`stable_id` stability across code movement** | **planned** | Сохранять identity finding при сдвиге/лёгкой правке hunks, чтобы fix-tracking и sticky comments не «дрожали» |
| **Fix verification on later pushes** | **planned** | Фокусная перепроверка открытых finding бота -> `resolved` / `still_open` / `needs_human` |
| **Queue-backlog alert** | **planned** | Дополнение к worker-down scraping: рост ARQ backlog / desired-vs-running gap (отмечено в `observability/alerts.yml`) |
| **SECURITY.md contact channel** | **planned** | Добавить security contact e-mail или явно зафиксировать, что единственный канал — GitHub private advisories |
| **Webhook delivery deduplication** | **planned** | Запоминать `X-GitHub-Delivery` до вызовов GitHub |
| **Narrower installation tokens** | **planned** | Scope App installation tokens на reviewed repo / step |
| **Secret scanning of model output** | **planned** | Последняя линия защиты перед store/publish |
| **Grafana per-owner dimension** | **planned** | Dashboards/alerts с разрезом по `owner` |
| **Observability hardening** | **planned** | Корреляция стадий (`review_run_id`), SLOs, прогон алертов, подтверждение что логи/dashboards не светят tokens / PR code / issue text |

<!-- section: platform-expansion-later -->
### Platform expansion (later)

| Item | Status | Notes |
| --- | --- | --- |
| **`IssueTracker` contract + Jira update policy** | **planned** | Явно когда comments/transitions разрешены; audit; no-write preview; создание issues — отдельное решение |
| **`CodeHost` contract + second host (e.g. GitLab)** | **planned** | По спросу; общие contract tests |
| **`ReviewerRunner` contract + second CLI** | **planned** | Capability matrix; versioned finding format независимо от вендора |
| **Independent cross-validation** | **planned** | Второй runner confirms/disputes findings; caps по cost/concurrency; human на ambiguous P0/P1 |
| **Locale catalog + channel templates** | **planned** | Versioned шаблоны PR/Jira/Slack/digest; English fallback |
| **Owners in DB / hot reload / quotas** | **planned** | Дальше file/env owner registry |

<!-- section: directions-backlog -->
### Directions (backlog, not scheduled)

Идеи, пока не перенесённые в таблицу выше со статусом:

1. **Team rules in the repository** (`.autoreview/rules.md` с base branch).
2. **Local check before push** (CLI / pre-push, no-publish mode).
3. **Repository engineering health report** (в основном non-LLM scheduled audit).
4. **Spec-driven review** (долгосрочно; two-pass #20 — первый конкретный шаг).
5. **Feedback on findings** (useful / false-positive reactions -> stats).
6. **Strict profile for migrations / infra PRs.**
7. **Dependency / CVE check when lockfiles change.**

<!-- section: rules-that-still-apply -->
### Правила, которые остаются в силе

- New integrations need contract, failure/retry, and permission tests before sample config.
- Fixtures stay fictional; PR diffs and issues stay out of public logs/telemetry.
- Every automatic Jira or code-host write has an idempotency key, an audit record, and an enable switch.
- Public docs distinguish shipped, experimental, and planned behaviour.
- Model output schema is language-independent; templates never change severity, ids, paths, or machine statuses.
