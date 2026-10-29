<!-- section: roadmap-autoreview-bot -->
# Roadmap: autoreview-bot

Живой план self-hosted бота ревью PR. Канонична [английская версия](../ROADMAP.md); этот файл — зеркало.

Статусы planned work: **planned** / **design** / **in progress** / **testing** / **done**.

<!-- section: current-state -->
## 1. Текущее состояние

Что работает на `main` сегодня и честные ограничения по областям.

| Компонент | Что работает | Ограничения |
| --- | --- | --- |
| Конвейер ревью | GitHub PR → ARQ worker → Codex CLI → JSON schema → sticky comment; опциональный progress check run / commit status; опциональный assist для описания PR (`comment` / `fill_empty` / `append`) | Ещё нет политики rewrite низкого качества ([#20](https://github.com/maxsharlaev/autoreview-bot/issues/20)); усиленная верификация model output — в плане |
| Параллелизм | Per-PR advisory lock, deterministic job ids, conditional run claim, `HEAD_CHANGED` на pickup; private repos перепроверяют head перед publish | Public-repo publish path ещё без той же pre-publication head-гарантии; e2e-тесты Redis/ARQ queue-recovery и publish-failure всё ещё в плане |
| Multi-owner | Несколько GitHub owners в одном деплое: routing, per-owner GitHub / webhook / API / Jira / Slack / policies / model key ([MULTI_OWNER.md](MULTI_OWNER.md), #9 done) | Owners в БД / hot reload / quotas не сделаны; нет admin UI |
| Public repos | Редактура security findings и контроль Jira disclosure ([PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md)) | Пустой `github.allowed_repos` по-прежнему пускает все reachable repos (только startup warning) |
| Sandbox / изоляция агента | Fail-closed Linux sandbox; `shell_environment_policy` + execpolicy denylist; model key не наследуется agent children; AR-001/AR-002 приняты как platform limits ([SECURITY.md](../../SECURITY.md)) | Нет default-deny внутри read-only sandbox; unmatched in-sandbox tools всё ещё могут читать checkout |
| ReviewExecutor / container path | **M1 + M2 done** на `main` ([#26](https://github.com/maxsharlaev/autoreview-bot/pull/26), [#27](https://github.com/maxsharlaev/autoreview-bot/pull/27)): `codex.runner_backend` с default `in_process`; opt-in `container` через dedicated launcher (изоляция сокета, hard-coded create spec, leases, cancel kill-first) | Default остаётся `in_process`; model key всё ещё попадает в review process до M3; launcher cancel под нагрузкой Docker может пережить worker DELETE read timeout ([#29](https://github.com/maxsharlaev/autoreview-bot/issues/29)) |
| Observability | API/worker Prometheus metrics, alert rules (в т.ч. `OpenPrReviewQueueNotDraining`), Grafana dashboard; opt-in Compose overlay для Prometheus, Grafana, Loki и Alloy ([OBSERVABILITY.md](OBSERVABILITY.md)) | Отдельного алерта на растущий backlog / desired-vs-running ещё нет; stage correlation / SLOs / alert exercise — в плане |
| Ops / admin | Status API, logs, Grafana; unit-тесты и Postgres migration/isolation в CI; lint/format/image build; docs coverage test | Нет admin UI; triage findings (false positive / accepted risk) вручную; local pre-commit hooks, drill'ы load/backup/upgrade/rotation и clean-install validation всё ещё в плане |
| Jira | Read + опциональный comment / P0–P1 transition | Model severity всё ещё может двигать writes без human gate |
| Платформа | Один code host и reviewer CLI (GitHub + Codex); EN+RU docs; Compose overlays | Нет второго host/runner; примеры hardening деплоя (закрытые порты, TLS, protected metrics) всё ещё в плане |
| Security reporting | GitHub private advisories | Альтернативный e-mail contact всё ещё TODO в `SECURITY.md` |

<!-- section: planned-work -->
## 2. Запланированная работа

<!-- section: near-term -->
### Ближайший таргет

| Пункт | Status | Заметки |
| --- | --- | --- |
| **Admin panel (runs, queue, finding management)** | **planned** | **Ближайший таргет.** Optional (`ADMIN_UI_ENABLED=false`); queue/runs, filters, safe retry, audit log. Finding management: помечать false positives и accepted risk так, чтобы они **не поднимались снова** на следующих push; cleanup sensitive comments (`has_sensitive_content`). Authz отдельно от public API |

<!-- section: container-strict-review-path -->
### Container / строгий путь ревью

| Пункт | Status | Заметки |
| --- | --- | --- |
| **M1 - ReviewExecutor + `in_process`** | **done** | [#26](https://github.com/maxsharlaev/autoreview-bot/pull/26) на `main` |
| **M2 - Ephemeral review container + launcher** | **done** | [#27](https://github.com/maxsharlaev/autoreview-bot/pull/27) на `main`: launcher-only Docker socket, hard-coded create spec, leases, fail closed |
| **Launcher cancel bounded under worker DELETE timeout** | **planned** | [#29](https://github.com/maxsharlaev/autoreview-bot/issues/29): ограничить весь cancel (list/kill/remove), не только lifecycle wait, чтобы worker DELETE не таймаутился под нагрузкой Docker |
| **M3 - Model-API egress proxy + strict egress allowlist** | **design** | Убрать model key из review container |
| **M4 - Container backend as default** | **planned** | После M3: сделать `runner_backend=container` default |
| **Linux-host AppArmor for review containers** | **planned** | Docker Desktop: N/A; Linux hosts: dedicated profile вместо default/unconfined follow-up |

<!-- section: product-ux -->
### Продукт / UX

| Пункт | Status | Заметки |
| --- | --- | --- |
| **PR description / title policy + rewrite** | **planned** | [#20](https://github.com/maxsharlaev/autoreview-bot/issues/20): `pr_description.policy` / `title_policy`, per-repo overrides, deterministic low-quality checks, rewrite с сохранением author text |
| **Two-pass review (spec then verify)** | **planned** | Часть #20 / spec-driven direction: pass 1 пишет spec; pass 2 в fresh session сверяет code vs spec (и Jira при связи); отдельно репортит code defects и inconsistencies |
| **Human approval for model-driven Jira actions** | **planned** | Propose P0/P1 transition/comment; confirm через label / reaction / admin panel |

<!-- section: reliability-security-observability -->
### Надёжность / security / observability

| Пункт | Status | Заметки |
| --- | --- | --- |
| **Deny-by-default empty allowlist** | **planned** | Reject empty `github.allowed_repos` (или equivalent) вместо warn-and-allow |
| **Head recheck before publish (all repos)** | **planned** | Распространить private-repo гарантию на public publish path |
| **Stronger model-output verification** | **planned** | Reject wrong SHA, paths outside the diff, impossible lines, inconsistent previous-finding status; mark incomplete coverage |
| **`stable_id` stability across code movement** | **planned** | Держать finding identity стабильным при сдвиге/лёгкой правке hunks, чтобы fix-tracking и sticky comments не «прыгали» |
| **Fix verification on later pushes** | **planned** | Focused re-check open bot findings → `resolved` / `still_open` / `needs_human` |
| **Queue-backlog alert** | **planned** | **Отдельно** от существующего `OpenPrReviewQueueNotDraining` (worker-down / scrape gap): детект растущего ARQ backlog или desired-vs-running при живых workers (заметка в `observability/alerts.yml`) |
| **SECURITY.md contact channel** | **planned** | Добавить security contact e-mail или явно зафиксировать, что единственный канал — GitHub private advisories |
| **Webhook delivery deduplication** | **planned** | Запоминать `X-GitHub-Delivery` до GitHub calls |
| **Narrower installation tokens** | **planned** | Scope App installation tokens на reviewed repo / step |
| **Secret scanning of model output** | **planned** | Last line of defense перед store/publish |
| **Grafana per-owner dimension** | **planned** | Dashboards/alerts с разрезом по `owner` |
| **Observability hardening** | **planned** | Stage correlation (`review_run_id`), SLOs, alert exercise, подтвердить что logs/dashboards не светят tokens / PR code / issue text |

<!-- section: quality-ops-deployment -->
### Качество / ops / деплой

Сохранённые обязательства из прежнего помесячного плана (тесты Month 1 и exit Month 5 public beta).

| Пункт | Status | Заметки |
| --- | --- | --- |
| **Redis/ARQ queue-recovery tests** | **planned** | Integration-покрытие Redis/ARQ: drain очереди после рестарта worker, delayed jobs и восстановление после сбоев брокера |
| **Publish-failure tests** | **planned** | E2E-сценарии, где sticky comment / check-run / Jira publish падает mid-flight; состояние run и retries остаются корректными |
| **Local pre-commit checks** | **planned** | Локальные hooks в линии с CI: Ruff lint/format, валидность YAML/JSON/TOML, secrets, large files, conflict markers, whitespace |
| **Load testing** | **planned** | Устойчивая нагрузка ревью, cancellation под нагрузкой и queue recovery (exit Month 5) |
| **Backup / restore testing** | **planned** | Drill'ы backup и restore БД как часть проверенного recovery path |
| **Schema-upgrade testing** | **planned** | Alembic upgrades/downgrades на реалистичных данных сверх существующих unit-тестов миграций |
| **Token rotation drills** | **planned** | Ротация GitHub / Jira / Slack / launcher / model credentials и проверка, что стек поднимается без сюрпризов по downtime |
| **Deployment hardening examples** | **planned** | Примеры Compose/docs с закрытыми портами database/Redis, protected metrics, TLS и минимальными правами интеграций |
| **Clean-install validation** | **planned** | Установка с чистой машины на новый проект (GitHub + Jira, затем второй host/runner когда будет); опубликовать compatibility matrix и guidance по миграции конфигурации; проверить отключение интеграций |

<!-- section: platform-expansion-later -->
### Расширение платформы (позже)

| Пункт | Status | Заметки |
| --- | --- | --- |
| **`IssueTracker` contract + Jira update policy** | **planned** | Явно когда comments/transitions разрешены; audit; no-write preview; issue creation — отдельное решение |
| **`CodeHost` contract + second host (e.g. GitLab)** | **planned** | Demand-driven; shared contract tests |
| **`ReviewerRunner` contract + second CLI** | **planned** | Capability matrix; versioned finding format независимо от vendor |
| **Independent cross-validation** | **planned** | Второй runner confirms/disputes findings; cost/concurrency caps; human на ambiguous P0/P1 |
| **Locale catalog + channel templates** | **planned** | Versioned PR/Jira/Slack/digest templates; English fallback |
| **Owners in DB / hot reload / quotas** | **planned** | Дальше file/env owner registry |

<!-- section: directions-backlog -->
### Направления (бэклог, без расписания)

Идеи, пока не подняты в таблицы Planned work со статусом. Admin panel **здесь не числится** — это near-term target в Planned work.

1. **Правила команды в репозитории** (`.autoreview/rules.md` из base branch).
2. **Локальная проверка перед push** (CLI / pre-push, no-publish mode).
3. **Отчёт об инженерном здоровье репозитория** (в основном non-LLM scheduled audit).
4. **Spec-driven review** (long-term; two-pass #20 — первый конкретный шаг).
5. **Обратная связь по findings** (useful / false-positive reactions → stats; near-term путь — admin finding management).
6. **Строгий профиль для migrations / infra PR.**
7. **Проверка зависимостей / CVE при смене lockfiles.**

<!-- section: rules-that-still-apply -->
### Правила, которые по-прежнему действуют

- Новые интеграции нуждаются в contract-, failure/retry- и permission-тестах до sample config.
- Фикстуры остаются вымышленными; PR diffs и issues не попадают в public logs/telemetry.
- Каждая автоматическая запись в Jira или code host имеет idempotency key, audit record и enable switch.
- Публичные docs различают shipped, experimental и planned behaviour.
- Schema model output не зависит от языка; templates никогда не меняют severity, ids, paths или machine statuses.
