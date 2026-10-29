<!-- section: roadmap-autoreview-bot -->
# Roadmap: autoreview-bot

A five-month sequence of verifiable milestones, not fixed release dates: each stage ends with a checkable result. The service is meant for any self-hosting team. GitHub, Jira and Codex are the first implementations but do not define the internal interfaces of future integrations. The English roadmap is canonical; see the [Russian translation](ru/ROADMAP.md).

<!-- section: current-state -->
## Current state

| Area | Available | Gap |
| --- | --- | --- |
| Review | GitHub PR → ARQ → Codex CLI → JSON → sticky comment; optional PR description drafts; progress status ([PROGRESS_STATUS.md](PROGRESS_STATUS.md)) | Head SHA recheck before publication for public repositories, stronger output validation, end-to-end tests |
| Concurrency | Per-PR advisory lock, deterministic job ids, conditional run claim, `HEAD_CHANGED` at pickup ([API.md](API.md#concurrency-guarantees)) | Queue recovery and publish-failure scenarios in end-to-end tests |
| Issues | Jira read, optional comment and P0/P1 status transition | Update policy, decision log and contract tests |
| Code hosts | GitHub App or PAT | Common interface and a second adapter |
| Reviewers | One Codex CLI run | Runner interface, second CLI and independent validation |
| Observability | API/worker metrics, alerts, dashboard, logs | Stage correlation, SLOs, alert exercise and data exposure review |
| Operations | Run status API and queue snapshot | Optional authenticated task dashboard |
| Language and prompts | English defaults; `en`/`ru` fields and comment labels; configurable prompt files | Locale catalog, channel templates, per-repository language |
| Owners | Several GitHub owners in one deployment: routing, per-owner GitHub credentials, webhook secrets, API keys, Jira, Slack, policies and model key ([#9](https://github.com/maxsharlaev/autoreview-bot/issues/9), [MULTI_OWNER.md](MULTI_OWNER.md)) | Owners in the database or an admin UI, hot reload, per-owner quotas |
| Security | Mandatory webhook signatures, scoped API keys, public-repository redaction ([SECURITY.md](../SECURITY.md)) | Deny-by-default for an empty allowlist |
| Quality | Unit tests; Postgres migration and isolation tests in CI; lint/format/image build; docs coverage test | Redis/ARQ and external contract tests, local pre-commit hooks |

<!-- section: month-0-prepare-the-public-repository-1-2-weeks -->
## Month 0 — prepare the public repository (1–2 weeks)

1. Check licenses of dependencies and bundled assets, including Grafana templates, alongside the Apache-2.0 license.
2. Review published files, examples, documentation and CI for credentials and organization-specific data.
3. Public `CONTRIBUTING.md`, `SECURITY.md`, version support policy and a neutral sample configuration (done; keep current).
4. Check that the docs describe actual behaviour: capabilities, limits, webhook security, how to run, integration state. Record the base architecture and event flow.

**Exit:** an independent user can read the docs and deploy to a test repository; a repeated audit of the published repository finds no internal data.

<!-- section: month-1-reliable-review -->
## Month 1 — reliable review

1. Recheck the current PR head before persistence and publication for every repository; a cancelled or stale run must not change the comment or Jira. (Atomic deduplication of repeated and concurrent deliveries and the pickup check are done; the pre-publication check exists for private repositories.)
2. Reject model output with a wrong SHA, a path outside the diff, an impossible line or an inconsistent previous-finding status. Mark incomplete coverage explicitly.
3. Deny by default when the repository allowlist is empty. Test author permissions, forks and sandbox boundaries. (Manual API authorization and mandatory webhook signatures are already separate.)
4. Tests: unit tests for finding transitions and the verifier; Postgres/Redis/ARQ integration; an end-to-end synthetic PR through fake GitHub/Jira APIs; a new SHA during review, a duplicate webhook, a publish failure and queue recovery.
5. Local pre-commit hooks and matching CI checks: Ruff lint/format, YAML/JSON/TOML validity, secrets, large files, conflict markers and whitespace.
6. Separate structured findings and statuses from published text. English prompt and comment defaults exist; remove remaining hardcoded output text and define compatibility with older mixed-language output.

**Exit:** one PR and one SHA give a predictable state; a stale run does not publish; the test pipeline reproduces locally and in CI.

<!-- section: month-2-issue-tracker-and-observability -->
## Month 2 — issue tracker and observability

1. Define `IssueTracker`: key lookup, fields and acceptance criteria, comment and an allowed status transition. Jira is the first implementation.
2. Specify when Jira updates are allowed: which results need a comment, when a transition is allowed, missing permissions, closed issues, project mismatches and repeated delivery. Record each operation and its reason. Issue creation is a separate product decision.
3. Anonymized Jira contract tests, an end-to-end `PR → issue → review → comment/transition` scenario and a no-write preview mode.
4. Observability review: metric names, units and cardinality; correlate `review_run_id` across stages and logs; measure queue, model and publication time, connector errors and incomplete coverage. Set SLOs, exercise alerts with injected failures, and check that logs and dashboards expose no tokens, PR code or issue text.
5. A locale catalog and versioned output templates for PR, Jira, Slack and digest: installation or repository language, English fallback, template preview and safe escaping of untrusted text. Render tests for a clean PR, P0/P1, failures and partial coverage in each language.

**Exit:** operators see issue state and the reasons for actions; Jira can be checked without side effects; alerts point to the failing stage; English works without configuration and localized templates pass snapshot tests.

<!-- section: month-3-interchangeable-hosts-and-clis -->
## Month 3 — interchangeable hosts and CLIs

1. A `CodeHost` contract: PR/MR events, diff and revisions, clone/auth, permissions, publication and duplicate delivery. Move the GitHub adapter onto it without public behaviour changes.
2. A second host, such as GitLab, based on demand and available permissions, with the same contract tests for both.
3. A `ReviewerRunner` contract: model version, input context, limits, cancellation, timeout, cost/tokens and structured output. Move Codex onto it; add a second CLI with its own tests and a capability matrix.
4. Version the normalized finding format and run events independently of one vendor's schema.

**Exit:** the same review runs through two code hosts and two reviewer CLIs; unsupported features are explicit in the configuration.

<!-- section: month-4-independent-validation-and-an-optional-admin-ui -->
## Month 4 — independent validation and an optional admin UI

1. Cross-validation: the main runner produces a verdict and evidence; a second runner gets the code, diff and verdict and confirms, disputes or refers each finding to a human. Grok is a candidate second provider after checking API/CLI availability, data terms and cost.
2. Store separate responses, model versions, disagreement reasons and the final decision. A validator never confirms its own output; ambiguous P0/P1 go to a human. Cap cost, runtime and concurrency.
3. An optional admin UI (`ADMIN_UI_ENABLED=false` by default): queue and runs, filters by host/repository/status, stage details, safe retry and an audit log. Separate authentication and roles; limit exposure of code, issues and secrets. The status API keeps working without the UI. English first, using the same locale catalog.
4. Evaluate on anonymized PRs: P0/P1 precision, false positives, coverage, model agreement, latency and cost, before enabling validation broadly.

**Exit:** operators follow the task lifecycle in a UI that can be switched off; independent validation adds an explainable result at a measurable cost.

<!-- section: month-5-public-beta -->
## Month 5 — public beta

1. Install from a clean machine with a new project: GitHub and Jira, then the second host and runner. Publish a compatibility matrix and configuration migration guidance.
2. Test load, cancellation and queue recovery, database backups, schema upgrades, token rotation and disabling integrations.
3. Deployment examples with closed database/Redis ports, protected metrics, TLS and minimal integration permissions.
4. Decide separately, using quality data, whether a blocking check is justified. Until then reviews stay advisory.

**Exit:** a public beta with documented limits, reproducible setup and a tested recovery path.

<!-- section: planned-follow-ups -->
## Planned follow-ups

Smaller items already agreed, independent of the monthly plan:

- **Linux-host AppArmor for review containers:** Docker Desktop does not enforce AppArmor; on Linux hosts, add a dedicated profile instead of leaving the default.
- **Webhook delivery deduplication:** remember `X-GitHub-Delivery` ids so a redelivered event is recognized before any GitHub call.
- **Narrower installation tokens:** request App installation tokens scoped to the reviewed repository and only the permissions a step needs.
- **Grafana per owner:** an `owner` dimension in dashboards and alerts, so each owner's queue, failures and cost are visible separately.
- **Ephemeral per-review container (strict path):** run each review in a short-lived container with **no worker secrets** (checkout + one-shot model credential / later model-API proxy). **M1 (done):** `ReviewExecutor` + `codex.runner_backend`. **M2 (done, opt-in):** dedicated **launcher** holds the Docker socket and starts review containers from a hard-coded spec; worker sends `{job_id, checkout_ref}` only; model key via process env; fail closed to `CODEX_SANDBOX`. **M3:** model-API egress proxy + strict egress allowlist; then default `runner_backend=container`. Key via env policy in M2; wrappers/`noexec` are not the path. See [SECURITY.md](../SECURITY.md) and [ARCHITECTURE.md](ARCHITECTURE.md#review-executor-backends).
- **Secret scanning of model output:** scan model output for secrets before it is stored or published, as a last line of defense.
- **Human approval for model-driven Jira actions:** Jira transitions and comments triggered by model-assigned severity (P0/P1) are proposed instead of applied, and a person confirms them through a PR label, a reaction or the admin panel. Configurable per owner (apply automatically, require approval, or off).
- **Admin panel with sensitive-comment cleanup:** list runs whose published comment had sensitive content (`has_sensitive_content`) and let an operator redact or delete those comments, for example before a repository becomes public ([PROGRESS_STATUS.md](PROGRESS_STATUS.md#has_sensitive_content)).

<!-- section: future-directions -->
## Future directions

Ideas beyond the plan above, in priority order. Sizes are rough engineering estimates for one developer.

<!-- section: 1-fix-verification -->
### 1. Fix verification

- **Goal:** show whether the bot's findings actually get fixed, and close them automatically when they are.
- **Sketch:** on a new push, run a focused check on only the bot's open findings from earlier runs (location, evidence, the new diff) and mark each one `resolved`, `still_open` or `needs_human`. Resolved findings are collapsed in the sticky comment. A metric counts findings fixed per severity and category, plus time to fix.
- **Risks:** the model may mark a finding resolved when the code only moved; an extra model call per push adds cost. Mitigations: require evidence of the change, and make the check cheaper than a full review.
- **Size:** about 1 week.

<!-- section: 2-team-rules-in-the-repository -->
### 2. Team rules in the repository

- **Goal:** review against the team's own conventions, not only general best practice.
- **Sketch:** an optional `.autoreview/rules.md` in the reviewed repository, read from the PR's **base** branch (so a PR can't change its own rules), size-limited and passed to the model as untrusted review criteria. Findings can cite the rule they apply. Per-owner config can turn the feature on or off.
- **Risks:** prompt injection through the rules file (it stays in an untrusted block, and its rules can't change output format or publication); long files add tokens; vague rules cause noise.
- **Size:** 3–5 days.

<!-- section: 3-local-check-before-push -->
### 3. Local check before push

- **Goal:** let a developer get a review in the terminal before pushing.
- **Sketch:** a small CLI and an optional git `pre-push` hook. It sends the staged changes, a commit or the branch diff against its base to the API in a **no-publish** mode: nothing is posted to GitHub, Jira or Slack, and the result comes back to the terminal. Each developer gets their own key with a cost limit and rate limit, scoped to an owner.
- **Data handling:** code leaves the developer's machine and goes to the service and the model provider. The CLI applies an ignore list before sending (secrets, `.env*`, keys, generated and vendored files), shows what will be sent, and the server applies its skip rules. Results are not stored as PR findings.
- **Risks:** secret leakage from a local diff; cost abuse; results differing from the PR review.
- **Size:** about 1 week.

<!-- section: 4-repository-engineering-health-report -->
### 4. Repository engineering health report

- **Goal:** a periodic picture of a repository's engineering health, beyond single PRs.
- **Sketch:** a scheduled audit built mostly from non-LLM tools: outdated and vulnerable dependencies; test coverage and flaky tests; hotspots by churn × findings; complexity and dead code; CI duration and failure rate; docs drift against code (for example config keys or env vars missing from docs). It uses the owner's findings history. An LLM only synthesizes a short prioritized summary. Output goes to the future admin panel and optionally to Slack.
- **Risks:** tools differ by language and ecosystem; report noise; access to CI data needs extra permissions; cost on large repositories.
- **Size:** 2–3 weeks for an MVP.

<!-- section: 5-spec-driven-review-long-term-direction -->
### 5. Spec-driven review (long-term direction)

- **Goal:** move from "does the code have problems" to "does the code do what was intended", as a step toward managing AI-assisted development. No dates: this moves slowly, in small steps, as a feature of the current bot.
- **Stages:**
  1. **Specs as repository artifacts:** specifications in the reviewed repository (for example `specs/*.md`) with checkable acceptance criteria, each with an id, a statement and how to verify it. A PR references a spec or specific criteria.
  2. **PR checked against the spec:** a verdict per criterion (`met`, `not met`, `not touched`) with evidence, reusing the existing findings model as a new category.
  3. **Spec coverage over time:** which criteria are implemented, which are covered by tests, and where the code deviates from the spec.
  4. **Closed loop with coding agents:** spec → tasks → agent PR → the bot checks the criteria → unmet criteria go back to the agent.
- **Risks and notes:** model nondeterminism, so verdicts need repeat runs or consensus, and deterministic checks such as tests wherever possible; spec quality, so the bot should first check that criteria are verifiable; scope, so start narrow inside the current bot rather than as a separate product.

<!-- section: 6-control-over-pr-title-and-description-edits -->
### 6. Control over PR title and description edits

- **Goal:** let each owner and repository decide whether the bot edits a PR's title and description, and fix poor ones (for example a title and body that GitHub truncated from a single commit subject) without ever losing the author's text. This is the first concrete step toward [spec-driven review](#5-spec-driven-review-long-term-direction).
- **Policy:** `pr_description.policy` is `off`, `suggest` (a separate comment, like today's `comment` mode), `fill_empty`, `rewrite_if_low_quality` or `always`, with a separate policy for the title. The existing `mode` and `title_mode` map onto it. Overrides go global → owner → repository, and an opt-out label turns edits off for one PR.
- **Low-quality checks:** deterministic checks run first and cost nothing: GitHub truncation (title ending in `…` with the body starting with `…`), a title equal to the only commit subject, a placeholder title, an empty or template-only body, a body that is too short. A model is asked only when these checks are not conclusive.
- **Safety:** the original title and body are kept in a collapsed block; the bot stores a hash of the body it wrote (`bot_body_hash`) next to `bot_title_source_hash`; a human edit after a bot edit makes the PR human-owned for good; the PR is re-read before every write; `always` is allowed only when set explicitly for a repository.
- **Two passes with a context break:** when edits are enabled, pass 1 writes a spec for the change (what to test and how). Pass 2 starts a fresh session with no pass-1 reasoning, only the spec artifact and the code, evaluates the code against the spec and reports two things separately: (a) defects in the code, (b) inconsistencies between the spec, the description or the task and the code. When the task is in Jira, pass 2 compares against the Jira issue.
- **Later idea:** separate, cheaper models per pass. We will come back to it.
- **Risks:** an extra model call per PR adds cost; a weak spec makes pass 2 noisy; rewriting text people care about needs the safety rules above to hold without exceptions.

<!-- section: smaller-ideas-unordered -->
### Smaller ideas (unordered)

- **Feedback on findings:** reactions on findings (useful / false positive) collected into false-positive statistics per category and severity, used to tune policies and prompts.
- **Strict profile for migrations and infrastructure:** stricter criteria when a PR changes Alembic migrations, Dockerfiles, Compose files or CI configs (downgrade paths, data loss, secrets, permissions).
- **Dependency check in PRs:** when lock files change, check new or updated packages for known CVEs and typosquatting, and report them as findings.

<!-- section: rules-throughout -->
## Rules throughout

- A new integration passes contract, failure/retry and permission tests before it enters the sample configuration.
- Fixtures use fictional organizations, repositories, issues and tokens. PR diffs and issues stay out of public logs and telemetry.
- Every automatic Jira or code-host write has an idempotency key, an audit record and an enable switch.
- Public docs distinguish shipped, experimental and planned behaviour.
- The model output schema is independent of display language; translations and templates never change severity, ids, paths or machine statuses.
