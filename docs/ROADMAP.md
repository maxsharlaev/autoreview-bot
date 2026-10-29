<!-- section: roadmap-autoreview-bot -->
# Roadmap: autoreview-bot

Living plan for the self-hosted PR review bot. The English roadmap is canonical; see the [Russian translation](ru/ROADMAP.md).

Statuses for planned work use: **planned** / **design** / **in progress** / **testing** / **done**.

<!-- section: current-state -->
## 1. Current state

What works today on `main` (and what does not).

<!-- section: works-today -->
### Works today

| Area | Status |
| --- | --- |
| Review pipeline | GitHub PR -> ARQ worker -> Codex CLI -> JSON schema -> sticky comment; optional progress check run / commit status |
| Concurrency | Per-PR advisory lock, deterministic job ids, conditional run claim, `HEAD_CHANGED` at pickup; private repos re-check head before publish |
| Multi-owner | Several GitHub owners in one deployment: routing, per-owner GitHub / webhook / API / Jira / Slack / policies / model key ([MULTI_OWNER.md](MULTI_OWNER.md), #9 done) |
| Public repos | Redaction of security findings and Jira disclosure controls ([PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md)) |
| Sandbox / agent isolation | Fail-closed Linux sandbox; `shell_environment_policy` + execpolicy denylist; model key not inherited by agent children; AR-001/AR-002 accepted as platform limits ([SECURITY.md](../SECURITY.md)) |
| ReviewExecutor | **M1 done** on `main` ([#26](https://github.com/maxsharlaev/autoreview-bot/pull/26)): `codex.runner_backend` with `in_process` default; `container` is rejected at config load until M2 lands. **M2 testing** in [#27](https://github.com/maxsharlaev/autoreview-bot/pull/27) (not merged to `main` yet): opt-in ephemeral review containers via a dedicated launcher |
| Observability | API/worker Prometheus metrics, alert rules, Grafana dashboard; opt-in Compose overlay for Prometheus, Grafana, Loki, and Alloy ([OBSERVABILITY.md](OBSERVABILITY.md)) |
| PR description (basic) | Optional post-review title/body assist (`comment` / `fill_empty` / `append`); no low-quality rewrite policy yet |
| Docs / ops | EN+RU docs mirror; Compose overlays for observability |

<!-- section: honest-limitations -->
### Honest limitations

- **Default path is still `in_process`.** The container backend is **not on `main`**; M2 is testing in #27. Until M2 merges, only `in_process` runs. After M2, the model key still enters the review process until M3.
- **Codex platform gaps remain** for public repos: no default-deny inside the read-only sandbox; execpolicy is forbid-only; unmatched in-sandbox tools can still read the checkout.
- **Empty `github.allowed_repos` allows all reachable repos** (startup warning only) - deny-by-default is not shipped.
- **Pre-publication head recheck** is solid for private repos; public-repo publish path still needs the same guarantee called out as a gap.
- **No admin UI**; ops rely on the status API, logs, and Grafana.
- **Jira writes** (comment / P0-P1 transition) can still be driven by model severity without a human gate.
- **Single code host and single reviewer CLI** (GitHub + Codex); no second host/runner.
- **Test gaps:** Redis/ARQ and external contract tests, broader e2e of publish failure / queue recovery; local pre-commit hooks not standardized.
- **Security reporting contact:** GitHub private advisories only; alternate e-mail still a TODO in `SECURITY.md`.

<!-- section: planned-work -->
## 2. Planned work

<!-- section: container-strict-review-path -->
### Container / strict review path

| Item | Status | Notes |
| --- | --- | --- |
| **M1 - ReviewExecutor + `in_process`** | **done** | [#26](https://github.com/maxsharlaev/autoreview-bot/pull/26) on `main` |
| **M2 - Ephemeral review container + launcher** | **testing** | [#27](https://github.com/maxsharlaev/autoreview-bot/pull/27) (not on `main` yet): launcher-only Docker socket, hard-coded create spec, fail closed |
| **M3 - Model-API egress proxy + strict egress allowlist** | **design** | Remove model key from the review container |
| **M4 - Container backend as default** | **planned** | After M3: make `runner_backend=container` the default |
| **Linux-host AppArmor for review containers** | **planned** | Docker Desktop: N/A; Linux hosts: dedicated profile instead of default/unconfined follow-up |

<!-- section: product-ux -->
### Product / UX

| Item | Status | Notes |
| --- | --- | --- |
| **Admin panel (runs, queue, finding management)** | **planned** | Optional (`ADMIN_UI_ENABLED=false`); queue/runs, filters, safe retry, audit log; manage findings / sensitive-comment cleanup (`has_sensitive_content`); authz separate from the public API |
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
| **Fix verification on later pushes** | **planned** | Focused re-check of open bot findings -> `resolved` / `still_open` / `needs_human` |
| **Queue-backlog alert** | **planned** | Complement worker-down scraping: detect growing ARQ backlog / desired-vs-running gap (called out in `observability/alerts.yml`) |
| **SECURITY.md contact channel** | **planned** | Add a security contact e-mail or explicitly document that GitHub private advisories are the only channel |
| **Webhook delivery deduplication** | **planned** | Remember `X-GitHub-Delivery` before GitHub calls |
| **Narrower installation tokens** | **planned** | Scope App installation tokens to the reviewed repo / step |
| **Secret scanning of model output** | **planned** | Last line of defense before store/publish |
| **Grafana per-owner dimension** | **planned** | Dashboards/alerts split by `owner` |
| **Observability hardening** | **planned** | Stage correlation (`review_run_id`), SLOs, alert exercise, confirm logs/dashboards never expose tokens / PR code / issue text |

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

These stay ideas until pulled into the table above with a status:

1. **Team rules in the repository** (`.autoreview/rules.md` from base branch).
2. **Local check before push** (CLI / pre-push, no-publish mode).
3. **Repository engineering health report** (mostly non-LLM scheduled audit).
4. **Spec-driven review** (long-term; two-pass #20 is the first concrete step).
5. **Feedback on findings** (useful / false-positive reactions -> stats).
6. **Strict profile for migrations / infra PRs.**
7. **Dependency / CVE check when lockfiles change.**

<!-- section: rules-that-still-apply -->
### Rules that still apply

- New integrations need contract, failure/retry, and permission tests before sample config.
- Fixtures stay fictional; PR diffs and issues stay out of public logs/telemetry.
- Every automatic Jira or code-host write has an idempotency key, an audit record, and an enable switch.
- Public docs distinguish shipped, experimental, and planned behaviour.
- Model output schema is language-independent; templates never change severity, ids, paths, or machine statuses.
