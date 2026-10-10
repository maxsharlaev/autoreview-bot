# Roadmap: autoreview-bot

This is a five-month sequence of verifiable milestones, not fixed release dates. The service is intended for any self-hosting team. GitHub, Jira and Codex are the first implementations. See the [Russian translation](ru/ROADMAP.md); the English roadmap is canonical.

## Current state

| Area | Available | Gap |
| --- | --- | --- |
| Review | GitHub PR → ARQ → Codex CLI → JSON → sticky comment; optional PR description drafts | Final review SHA guard, stronger validation and end-to-end tests |
| Issues | Jira read, optional comment and P0/P1 status transition | Update policy, audit and contract tests |
| Code hosts | GitHub App or PAT | Common interface and second adapter |
| Reviewers | Codex CLI | Runner interface, second CLI and independent validation |
| Observability | API/worker metrics, alerts, dashboard, logs | Stage correlation, SLOs, alert exercise and data exposure review |
| Operations | Run status API and queue snapshot | Optional authenticated task dashboard |
| Language and prompts | English defaults; `en`/`ru` fields and comment labels; configurable prompt file | Channel templates, locale catalog, per-repository settings and full translation tests |
| Owners | Several GitHub owners in one deployment: routing, per-owner GitHub credentials, webhook secrets, API keys, Jira, Slack, policies and model key ([#9](https://github.com/maxsharlaev/autoreview-bot/issues/9)) | Owners in the database or an admin UI, hot reload, per-owner quotas |
| Quality | Unit tests and CI lint/format/test/image build | Database/queue/contract tests and local pre-commit hooks |

## Month 0 — prepare public repository (1–2 weeks)

1. Check licenses of dependencies and bundled assets, including Grafana templates, alongside the Apache-2.0 license.
2. Review release files, examples, documentation and CI for credentials and organization-specific data.
3. Add `CONTRIBUTING.md`, `SECURITY.md`, version support policy and neutral sample configuration.
4. Check that README describes actual behavior, especially webhook authorization, integrations and current limits.

**Exit:** an independent user can deploy to a test repository; an audit of the repository to be published finds no internal data.

## Month 1 — reliable review

1. Check the current PR head SHA before persistence and publication. Make repeated webhooks and concurrent runs atomic.
2. Reject model output with a wrong SHA, invalid diff path or line, or inconsistent previous-finding status. Mark incomplete coverage explicitly.
3. Deny access when the repository allowlist is empty; separate manual API authorization from mandatory GitHub webhook signature validation. Test author permissions, forks and sandbox boundaries.
4. Add Postgres/Redis/ARQ integration tests and a synthetic PR end-to-end test using fake GitHub/Jira APIs. Cover a changed SHA, duplicate webhook, publish failure and queue recovery.
5. Add pre-commit and matching CI checks for Ruff lint/format, YAML/JSON/TOML validity, secrets, large files, conflict markers and whitespace.
6. Separate structured findings from published text. English prompt and comment defaults are available; remove remaining hardcoded output text and define compatibility for existing mixed-language deployments.

**Exit:** a PR SHA has predictable state, stale runs do not publish, and local and CI checks agree.

## Month 2 — issue tracker and observability

1. Define `IssueTracker` operations for key lookup, issue/acceptance-criteria read, comment and allowed status transition; adapt Jira first.
2. Specify when Jira updates are allowed, including closed issues, permission failures, project mismatches and repeated delivery. Record each decision and result. Treat issue creation as a separate product decision.
3. Add anonymized Jira contract tests, an end-to-end PR → issue → review → update test and a no-write preview mode.
4. Review metric names, units and cardinality. Correlate `review_run_id` across stages and logs. Measure queue, model and publication time; connector errors and incomplete coverage. Set SLOs, exercise alerts and check logs and dashboards for tokens, PR code and issue text.
5. Build a locale catalog and versioned output templates for PR, Jira, Slack and digest. Support installation or repository locale, English fallback, preview and safe escaping. Test clean, blocked, failed and partial reviews in each language.

**Exit:** operators can explain issue updates and failures; localized channel templates are tested.

## Month 3 — interchangeable hosts and CLIs

1. Define a `CodeHost` contract for PR/MR events, diff/revision access, clone/auth, permissions, publication and duplicate delivery. Adapt GitHub without public behavior changes.
2. Add a second host, such as GitLab, based on demand and available permissions. Run the same contract tests against both.
3. Define `ReviewerRunner` inputs, model version, limits, cancellation, timeout, token/cost reporting and structured output. Adapt Codex and add a second CLI with a capability matrix.
4. Version normalized findings and run events independently of vendor schemas.

**Exit:** equivalent reviews run through two code hosts and two reviewer CLIs; unsupported features are explicit.

## Month 4 — independent validation and optional admin UI

1. Add cross-validation: a second runner receives code, diff and the first verdict and confirms, disputes or refers each finding to a human. Grok is a candidate after checking API/CLI availability, data terms and cost.
2. Store separate responses, model versions and disagreement reasons. Escalate ambiguous P0/P1 findings to people. Cap cost, runtime and concurrency.
3. Add an admin UI behind `ADMIN_UI_ENABLED=false`: queue, runs, filters, stage detail, safe retry and audit log. Use separate authentication and roles; limit exposure of source, issues and secrets. Keep the status API usable without the UI.
4. Evaluate P0/P1 precision, false positives, coverage, model agreement, latency and cost on anonymized PRs before enabling validation broadly.

**Exit:** operators can inspect tasks in an optional UI, and independent validation has a measurable benefit and cost.

## Month 5 — public beta

1. Test installation from a clean machine with a new project, GitHub/Jira and then the second host/runner. Publish compatibility and migration guidance.
2. Test load, cancellation, queue recovery, backups, schema upgrades, token rotation and disabling integrations.
3. Publish deployment examples with closed database/Redis ports, protected metrics, TLS and minimal integration permissions.
4. Decide separately, using quality data, whether a blocking check is justified. Until then reviews remain advisory.

**Exit:** a public beta with reproducible setup, documented limits and a tested recovery path.

## Rules throughout

- Each integration needs contract, failure/retry and permission tests before entering the sample configuration.
- Fixtures use fictional organizations, repos, issues and tokens. Raw PR diffs and issues stay out of public logs and telemetry.
- Automatic Jira/code-host writes need an idempotency key, audit record and enable switch.
- Public docs distinguish shipped, experimental and planned behavior.
- The model output schema stays independent of display language; translations never change severity, IDs, paths or machine statuses.
