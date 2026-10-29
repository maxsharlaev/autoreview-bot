<!-- section: roadmap-autoreview-bot -->
# Roadmap: autoreview-bot

Живой план self-hosted PR review-бота. Канонична [английская версия](../ROADMAP.md); этот файл — зеркало.

Статусы planned work: **planned** / **design** / **in progress** / **testing** / **done**.

<!-- section: current-state -->
## 1. Current state

Что работает на `main` сегодня и честные ограничения по областям.

| Component | What works | Limitations |
| --- | --- | --- |
| Review pipeline | GitHub PR → ARQ worker → Codex CLI → JSON schema → sticky comment; опциональный progress check run / commit status; опциональный PR description assist (`comment` / `fill_empty` / `append`) | Ещё нет политики rewrite низкого качества ([#20](https://github.com/maxsharlaev/autoreview-bot/issues/20)); усиленная верификация model output — в плане |
| Concurrency | Per-PR advisory lock, deterministic job ids, conditional run claim, `HEAD_CHANGED` на pickup; private repos перепроверяют head перед publish | Public-repo publish path ещё без той же pre-publication head-гарантии; пробелы e2e по Redis/ARQ и publish-failure / queue-recovery |
| Multi-owner | Несколько GitHub owners в одном деплое: routing, per-owner GitHub / webhook / API / Jira / Slack / policies / model key ([MULTI_OWNER.md](MULTI_OWNER.md), #9 done) | Owners в БД / hot reload / quotas не сделаны; нет admin UI |
| Public repos | Редактура security findings и контроль Jira disclosure ([PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md)) | Пустой `github.allowed_repos` по-прежнему пускает все reachable repos (только startup warning) |
| Sandbox / agent isolation | Fail-closed Linux sandbox; `shell_environment_policy` + execpolicy denylist; model key не наследуется agent children; AR-001/AR-002 приняты как platform limits ([SECURITY.md](../../SECURITY.md)) | Нет default-deny внутри read-only sandbox; unmatched in-sandbox tools всё ещё могут читать checkout |
| ReviewExecutor / container path | **M1 + M2 done** на `main` ([#26](https://github.com/maxsharlaev/autoreview-bot/pull/26), [#27](https://github.com/maxsharlaev/autoreview-bot/pull/27)): `codex.runner_backend` с default `in_process`; opt-in `container` через dedicated launcher (изоляция сокета, hard-coded create spec, leases, cancel kill-first) | Default остаётся `in_process`; model key всё ещё попадает в review process до M3; launcher cancel под нагрузкой Docker может пережить worker DELETE read timeout ([#29](https://github.com/maxsharlaev/autoreview-bot/issues/29)) |
| Observability | API/worker Prometheus metrics, alert rules (в т.ч. `OpenPrReviewQueueNotDraining`), Grafana dashboard; opt-in Compose overlay для Prometheus, Grafana, Loki и Alloy ([OBSERVABILITY.md](OBSERVABILITY.md)) | Отдельного алерта на растущий backlog / desired-vs-running ещё нет; stage correlation / SLOs / alert exercise — в плане |
| Ops / admin | Status API, logs, Grafana | Нет admin UI; triage findings (false positive / accepted risk) вручную |
| Jira | Read + опциональный comment / P0–P1 transition | Model severity всё ещё может двигать writes без human gate |
| Platform | Один code host и reviewer CLI (GitHub + Codex); EN+RU docs; Compose overlays | Нет второго host/runner; local pre-commit hooks не стандартизированы |
| Security reporting | GitHub private advisories | Альтернативный e-mail contact всё ещё TODO в `SECURITY.md` |

<!-- section: planned-work -->
## 2. Planned work

<!-- section: near-term -->
### Near-term (next target)

| Item | Status | Notes |
| --- | --- | --- |
| **Admin panel (runs, queue, finding management)** | **planned** | **Ближайший таргет.** Optional (`ADMIN_UI_ENABLED=false`); queue/runs, filters, safe retry, audit log. Finding management: помечать false positives и accepted risk так, чтобы они **не поднимались снова** на следующих push; cleanup sensitive comments (`has_sensitive_content`). Authz отдельно от public API |

<!-- section: container-strict-review-path -->
### Container / strict review path

| Item | Status | Notes |
| --- | --- | --- |
| **M1 - ReviewExecutor + `in_process`** | **done** | [#26](https://github.com/maxsharlaev/autoreview-bot/pull/26) на `main` |
| **M2 - Ephemeral review container + launcher** | **done** | [#27](https://github.com/maxsharlaev/autoreview-bot/pull/27) на `main`: launcher-only Docker socket, hard-coded create spec, leases, fail closed |
| **Launcher cancel bounded under worker DELETE timeout** | **planned** | [#29](https://github.com/maxsharlaev/autoreview-bot/issues/29): ограничить весь cancel (list/kill/remove), не только lifecycle wait, чтобы worker DELETE не таймаутился под нагрузкой Docker |
| **M3 - Model-API egress proxy + strict egress allowlist** | **design** | Убрать model key из review container |
| **M4 - Container backend as default** | **planned** | После M3: сделать `runner_backend=container` default |
| **Linux-host AppArmor for review containers** | **planned** | Docker Desktop: N/A; Linux hosts: dedicated profile вместо default/unconfined follow-up |

<!-- section: product-ux -->
### Product / UX

| Item | Status | Notes |
| --- | --- | --- |
| **PR description / title policy + rewrite** | **planned** | [#20](https://github.com/maxsharlaev/autoreview-bot/issues/20): `pr_description.policy` / `title_policy`, per-repo overrides, deterministic low-quality checks, rewrite с сохранением author text |
| **Two-pass review (spec then verify)** | **planned** | Часть #20 / spec-driven direction: pass 1 пишет spec; pass 2 в fresh session сверяет code vs spec (и Jira при связи); отдельно репортит code defects и inconsistencies |
| **Human approval for model-driven Jira actions** | **planned** | Propose P0/P1 transition/comment; confirm через label / reaction / admin panel |

<!-- section: reliability-security-observability -->
### Reliability / security / observability

| Item | Status | Notes |
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

<!-- section: platform-expansion-later -->
### Platform expansion (later)

| Item | Status | Notes |
| --- | --- | --- |
| **`IssueTracker` contract + Jira update policy** | **planned** | Явно когда comments/transitions разрешены; audit; no-write preview; issue creation — отдельное решение |
| **`CodeHost` contract + second host (e.g. GitLab)** | **planned** | Demand-driven; shared contract tests |
| **`ReviewerRunner` contract + second CLI** | **planned** | Capability matrix; versioned finding format независимо от vendor |
| **Independent cross-validation** | **planned** | Второй runner confirms/disputes findings; cost/concurrency caps; human на ambiguous P0/P1 |
| **Locale catalog + channel templates** | **planned** | Versioned PR/Jira/Slack/digest templates; English fallback |
| **Owners in DB / hot reload / quotas** | **planned** | Дальше file/env owner registry |

<!-- section: directions-backlog -->
### Directions (backlog, not scheduled)

Идеи, пока не подняты в таблицы Planned work со статусом. Admin panel **здесь не числится** — это near-term target в Planned work.

1. **Team rules in the repository** (`.autoreview/rules.md` из base branch).
2. **Local check before push** (CLI / pre-push, no-publish mode).
3. **Repository engineering health report** (в основном non-LLM scheduled audit).
4. **Spec-driven review** (long-term; two-pass #20 — первый конкретный шаг).
5. **Feedback on findings** (useful / false-positive reactions → stats; near-term путь — admin finding management).
6. **Strict profile for migrations / infra PRs.**
7. **Dependency / CVE check when lockfiles change.**

<!-- section: rules-that-still-apply -->
### Rules that still apply

- New integrations need contract, failure/retry, and permission tests before sample config.
- Fixtures stay fictional; PR diffs and issues stay out of public logs/telemetry.
- Every automatic Jira or code-host write has an idempotency key, an audit record, and an enable switch.
- Public docs distinguish shipped, experimental, and planned behaviour.
- Model output schema is language-independent; templates never change severity, ids, paths, or machine statuses.
