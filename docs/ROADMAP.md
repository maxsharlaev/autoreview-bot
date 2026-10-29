<!-- section: roadmap-autoreview-bot -->
# Roadmap: autoreview-bot

Living plan for the self-hosted PR review bot. The English roadmap is canonical; see the [Russian translation](ru/ROADMAP.md).

Statuses for planned work use: **planned** / **design** / **in progress** / **testing** / **done**.

<!-- section: current-state -->
## 1. Current state

What works today on `main`, and the honest limits of each area.

| Component | What works | Limitations |
| --- | --- | --- |
| Review pipeline | GitHub PR → ARQ worker → Codex CLI → JSON schema → sticky comment; optional progress check run / commit status; optional PR description assist (`comment` / `fill_empty` / `append`) | No low-quality rewrite policy yet ([#20](https://github.com/maxsharlaev/autoreview-bot/issues/20)); stronger model-output verification still planned |
| Concurrency | Per-PR advisory lock, deterministic job ids, conditional run claim, `HEAD_CHANGED` at pickup; private repos re-check head before publish | Public-repo publish path still needs the same pre-publication head guarantee; Redis/ARQ queue-recovery and publish-failure e2e tests still planned |
| Multi-owner | Several GitHub owners in one deployment: routing, per-owner GitHub / webhook / API / Jira / Slack / policies / model key ([MULTI_OWNER.md](MULTI_OWNER.md), #9 done) | Owners in DB / hot reload / quotas not shipped; no admin UI |
| Public repos | Redaction of security findings and Jira disclosure controls ([PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md)) | Empty `github.allowed_repos` still allows all reachable repos (startup warning only) |
| Sandbox / agent isolation | Fail-closed Linux sandbox; `shell_environment_policy` + execpolicy denylist; model key not inherited by agent children; AR-001/AR-002 accepted as platform limits ([SECURITY.md](../SECURITY.md)) | No default-deny inside the read-only sandbox; unmatched in-sandbox tools can still read the checkout |
| ReviewExecutor / container path | **M1 + M2 done** on `main` ([#26](https://github.com/maxsharlaev/autoreview-bot/pull/26), [#27](https://github.com/maxsharlaev/autoreview-bot/pull/27)): `codex.runner_backend` with `in_process` default; opt-in `container` via dedicated launcher (socket isolation, hard-coded create spec, leases, cancel kill-first) | Default remains `in_process`; model key still enters the review process until M3; launcher cancel under Docker load can outlive the worker DELETE read timeout ([#29](https://github.com/maxsharlaev/autoreview-bot/issues/29)) |
| Observability | API/worker Prometheus metrics, alert rules (incl. `OpenPrReviewQueueNotDraining`), Grafana dashboard; opt-in Compose overlay for Prometheus, Grafana, Loki, and Alloy ([OBSERVABILITY.md](OBSERVABILITY.md)) | No separate growing-backlog / desired-vs-running alert yet; stage correlation / SLOs / alert exercise still planned |
| Ops / admin | Status API, logs, Grafana; unit tests and Postgres migration/isolation tests in CI; lint/format/image build; docs coverage test | No admin UI; finding triage (false positive / accepted risk) is manual; local pre-commit hooks, load/backup/upgrade/rotation drills, and clean-install validation still planned |
| Jira | Read + optional comment / P0–P1 transition | Model severity can still drive writes without a human gate |
| Platform | Single code host and reviewer CLI (GitHub + Codex); EN+RU docs; Compose overlays | No second host/runner; deployment hardening examples (closed ports, TLS, protected metrics) still planned |
| Security reporting | GitHub private advisories | Alternate e-mail contact still a TODO in `SECURITY.md` |

<!-- section: planned-work -->
## 2. Planned work

<!-- section: near-term -->
### Near-term (next target)

| Item | Status | Notes |
| --- | --- | --- |
| **Admin panel (runs, queue, finding management)** | **planned** | **Nearest target.** Optional (`ADMIN_UI_ENABLED=false`); queue/runs, filters, safe retry, audit log. Finding management: mark false positives and accepted risk so they are **not re-raised** on later pushes; sensitive-comment cleanup (`has_sensitive_content`). Authz separate from the public API |

<!-- section: container-strict-review-path -->
### Container / strict review path

| Item | Status | Notes |
| --- | --- | --- |
| **M1 - ReviewExecutor + `in_process`** | **done** | [#26](https://github.com/maxsharlaev/autoreview-bot/pull/26) on `main` |
| **M2 - Ephemeral review container + launcher** | **done** | [#27](https://github.com/maxsharlaev/autoreview-bot/pull/27) on `main`: launcher-only Docker socket, hard-coded create spec, leases, fail closed |
| **Launcher cancel bounded under worker DELETE timeout** | **planned** | [#29](https://github.com/maxsharlaev/autoreview-bot/issues/29): bound the whole cancel (list/kill/remove), not only the lifecycle wait, so worker DELETE cannot time out under Docker load |
| **M3 - Model-API egress proxy + strict egress allowlist** | **design** | Remove model key from the review container |
| **M4 - Container backend as default** | **planned** | After M3: make `runner_backend=container` the default |
| **Linux-host AppArmor for review containers** | **planned** | Docker Desktop: N/A; Linux hosts: dedicated profile instead of default/unconfined follow-up |

<!-- section: product-ux -->
### Product / UX

| Item | Status | Notes |
| --- | --- | --- |
| **PR description / title policy + rewrite** | **planned** | [#20](https://github.com/maxsharlaev/autoreview-bot/issues/20): `pr_description.policy` / `title_policy`, per-repo overrides, deterministic low-quality checks, rewrite with author text preserved |
| **Two-pass review (spec then verify)** | **planned** | Part of #20 / spec-driven direction: pass 1 writes a spec; pass 2 fresh session checks code vs spec (and Jira when linked); reports code defects and inconsistencies separately |
| **Human approval for model-driven Jira actions** | **planned** | Propose P0/P1 transition/comment; confirm via label / reaction / admin panel |

<!-- section: reliability-security-observability -->
### Reliability / security / observability

| Item | Status | Notes |
| --- | --- | --- |
| **Deny-by-default empty allowlist** | **planned** | Reject empty `github.allowed_repos` (or equivalent) instead of warn-and-allow |
| **Head recheck before publish (all repos)** | **planned** | Extend private-repo guarantee to public publish path |
| **Stronger model-output verification** | **planned** | Reject wrong SHA, paths outside the diff, impossible lines, inconsistent previous-finding status; mark incomplete coverage |
| **`stable_id` stability across code movement** | **planned** | Keep finding identity stable when hunks move / lightly edit so fix-tracking and sticky comments do not churn |
| **Fix verification on later pushes** | **planned** | Focused re-check of open bot findings → `resolved` / `still_open` / `needs_human` |
| **Queue-backlog alert** | **planned** | **Separate** from existing `OpenPrReviewQueueNotDraining` (worker-down / scrape gap): detect a growing ARQ backlog or desired-vs-running gap while workers are up (called out in `observability/alerts.yml`) |
| **SECURITY.md contact channel** | **planned** | Add a security contact e-mail or explicitly document that GitHub private advisories are the only channel |
| **Webhook delivery deduplication** | **planned** | Remember `X-GitHub-Delivery` before GitHub calls |
| **Narrower installation tokens** | **planned** | Scope App installation tokens to the reviewed repo / step |
| **Secret scanning of model output** | **planned** | Last line of defense before store/publish |
| **Grafana per-owner dimension** | **planned** | Dashboards/alerts split by `owner` |
| **Observability hardening** | **planned** | Stage correlation (`review_run_id`), SLOs, alert exercise, confirm logs/dashboards never expose tokens / PR code / issue text |

<!-- section: quality-ops-deployment -->
### Quality / ops / deployment

Retained commitments from the previous month-based plan (Month 1 tests and Month 5 public-beta exit).

| Item | Status | Notes |
| --- | --- | --- |
| **Redis/ARQ queue-recovery tests** | **planned** | Integration coverage for Redis/ARQ: queue drain after worker restart, delayed jobs, and recovery after broker blips |
| **Publish-failure tests** | **planned** | End-to-end scenarios where sticky comment / check-run / Jira publish fails mid-flight; run state and retries stay correct |
| **Local pre-commit checks** | **planned** | Local hooks aligned with CI: Ruff lint/format, YAML/JSON/TOML validity, secrets, large files, conflict markers, whitespace |
| **Load testing** | **planned** | Sustained review load, cancellation under load, and queue recovery (Month 5 exit) |
| **Backup / restore testing** | **planned** | Database backup and restore drills as part of the tested recovery path |
| **Schema-upgrade testing** | **planned** | Alembic upgrades/downgrades against realistic data beyond the existing migration unit tests |
| **Token rotation drills** | **planned** | Rotate GitHub / Jira / Slack / launcher / model credentials and confirm the stack recovers without downtime surprises |
| **Deployment hardening examples** | **planned** | Compose/docs examples with closed database/Redis ports, protected metrics, TLS, and minimal integration permissions |
| **Clean-install validation** | **planned** | Install from a clean machine with a new project (GitHub + Jira, then second host/runner when available); publish a compatibility matrix and configuration migration guidance; exercise disabling integrations |

<!-- section: platform-expansion-later -->
### Platform expansion (later)

| Item | Status | Notes |
| --- | --- | --- |
| **`IssueTracker` contract + Jira update policy** | **planned** | Explicit when comments/transitions are allowed; audit; no-write preview; issue creation is a separate decision |
| **`CodeHost` contract + second host (e.g. GitLab)** | **planned** | Demand-driven; shared contract tests |
| **`ReviewerRunner` contract + second CLI** | **planned** | Capability matrix; versioned finding format independent of vendor |
| **Independent cross-validation** | **planned** | Second runner confirms/disputes findings; cost/concurrency caps; human on ambiguous P0/P1 |
| **Locale catalog + channel templates** | **planned** | Versioned PR/Jira/Slack/digest templates; English fallback |
| **Owners in DB / hot reload / quotas** | **planned** | Beyond file/env owner registry |

<!-- section: directions-backlog -->
### Directions (backlog, not scheduled)

These stay ideas until pulled into the tables above with a status. The admin panel is **not** listed here — it is the near-term target under Planned work.

1. **Team rules in the repository** (`.autoreview/rules.md` from base branch).
2. **Local check before push** (CLI / pre-push, no-publish mode).
3. **Repository engineering health report** (mostly non-LLM scheduled audit).
4. **Spec-driven review** (long-term; two-pass #20 is the first concrete step).
5. **Feedback on findings** (useful / false-positive reactions → stats; admin finding management is the near-term path).
6. **Strict profile for migrations / infra PRs.**
7. **Dependency / CVE check when lockfiles change.**

<!-- section: rules-that-still-apply -->
### Rules that still apply

- New integrations need contract, failure/retry, and permission tests before sample config.
- Fixtures stay fictional; PR diffs and issues stay out of public logs/telemetry.
- Every automatic Jira or code-host write has an idempotency key, an audit record, and an enable switch.
- Public docs distinguish shipped, experimental, and planned behaviour.
- Model output schema is language-independent; templates never change severity, ids, paths, or machine statuses.
