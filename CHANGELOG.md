# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Documentation restructure** (English primary, Russian mirror): a short
  README with a docs index (`docs/README.md`), and new reference pages
  `docs/CONFIGURATION.md` (every `config.yaml` key with type, default, range and
  per-owner override), `docs/ENVIRONMENT.md` (every environment variable and the
  service that reads it), `docs/API.md` (endpoints, fields and error codes),
  `docs/MULTI_OWNER.md`, `docs/UPGRADE.md`, `docs/DATA_HANDLING.md`,
  `docs/FAQ.md`, and English `docs/PROGRESS_STATUS.md` and
  `docs/PUBLIC_DISCLOSURE.md`. `docs/OPERATIONS.md` is now
  `docs/TROUBLESHOOTING.md` with a table of all error and skip codes.
  `SECURITY.md` gained a threat model, minimal permissions, secret handling and
  a hardening checklist; `CONTRIBUTING.md` covers database tests and migration
  rules. `tests/test_docs.py` checks that every config key and environment
  variable is documented and that relative links and anchors resolve.
  `.env.example` lists every variable; `RUN_MIGRATIONS`, which no code reads,
  is no longer set there.

- **Russian documentation mirror**: every English page in `docs/` now has a
  Russian counterpart in `docs/ru/` with the same sections in the same order
  (new: API, configuration, environment, data handling, FAQ, multiple owners,
  observability, upgrade, public release checklist; `docs/ru/OPERATIONS.md` is
  now `docs/ru/TROUBLESHOOTING.md`). `tests/test_docs.py` checks that the file
  sets and heading structures match.

- **Roadmap**: a long-term direction, spec-driven review (checking a PR against
  acceptance criteria in the repository), in `docs/ROADMAP.md` and its Russian
  mirror.

- **Review progress status** (#7): webhook runs report progress on the PR head
  as an `autoreview` check run (GitHub App, needs Checks: Read and write) or
  commit status (PAT, needs Commit statuses: Read and write). On by default
  (`progress_status.enabled: true`, `mode: auto`); updates are best effort, so
  deployments without the permission keep working. Findings never produce a
  failing check.
  A failed enqueue marks the run `failed` / `QUEUE_UNAVAILABLE` without
  creating a status; a disabled owner's statuses are closed as `skipped` with
  its own credentials. Statuses of an owner removed from config can't be closed
  on GitHub (documented).
- Manual runs (`POST /api/v1/reviews`) report the same progress status as
  webhook runs; `"report_status": false` opts a request out. A manual rerun on
  the same SHA closes the open status as `cancelled` before opening its own,
  and a superseded worker never writes to a closed status.
- `POST /api/v1/reviews` always reads the current PR head; a `head_sha` that is
  not the current head is rejected with `409` `stale_head_sha` (with
  `current_head_sha`) before anything is cancelled, stored or queued. The
  request's `base_sha` is no longer used. The worker closes a run as
  `HEAD_CHANGED` when the PR head moved before pickup instead of reviewing
  newer code under the old SHA.
- Enqueueing is serialized per PR (Postgres advisory lock). Before a manual
  request cancels an open run on a different SHA, it re-reads the PR head under
  the lock (10 s timeout): `409 stale_head_sha` if the head moved, `502` if the
  read fails, and nothing changes in either case.
- Waits around enqueueing are bounded. The per-PR lock is acquired within
  20 s, otherwise the webhook and manual endpoints answer `503 pr_queue_busy`
  (`Retry-After: 5`). Superseded jobs are aborted after the lock is released,
  best effort with a 2 s cap, and the worker now sets `allow_abort_jobs`.
  Redis job status checks are capped at 2 s.
- The per-PR lock no longer leaves a gap between committing a new run and
  enqueueing it: the arq job id (`review:<run id>`) is stored with the run, a
  fresh `pending` run (< 60 s) without a Redis job is treated as in flight
  (reused, or cancelled and aborted on `force`), and the worker only processes
  `pending` runs, claiming them with a conditional `pending -> running` update.
  A duplicate request can no longer start a second paid review for the same
  SHA, and a failed/cancelled/replaced run is never reviewed. Owner-reason
  skips taken before the claim (owner disabled, not configured, owner changed,
  route rejected) are conditional on `pending` too: a run another job already
  holds is left untouched and its progress status is not written.
- Migration `008_review_run_progress`: `review_runs.progress_status`,
  `comment_url` and `has_sensitive_content` (NULL for existing rows).
- **Multiple GitHub owners** ([#9](https://github.com/maxsharlaev/autoreview-bot/issues/9)): one deployment can
  serve several organizations or accounts through an optional `owners:` block. Each owner has its own GitHub
  App or PAT, webhook secret (`/api/v1/pull-request` checks all, `/api/v1/pull-request/<id>` one), scoped API
  key, Jira site and projects, Slack channel and digest, policy overrides (`public_repos`, `language`,
  `features`, `pr_description`, `size_guard`) and model settings (`codex.model`, `reasoning_effort`,
  `api_key_env`). Repositories are routed by explicit owner, `allowed_repos` (exact or `org/*`), installation
  id and `routing.unclaimed`. Integrations are never inherited between owners. API responses gain `owner`.
- Migrations `005_owner_id`, `006_comment_authors_objects` and `007_findings_owner_id` add `owner_id` to
  repositories, review runs, digest runs and findings, and store comment authors per owner. Downgrading `007`
  with findings of several owners on one PR requires `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1`.
- A config without `owners:` behaves as before (owner `default`). See the README section "Multiple owners".

### Changed

- **Review time in the comment**: the sticky review comment shows the reviewed
  commit and when the analysis finished, always in UTC, with its duration, for
  example `Reviewed: commit abc1234 at 2026-10-24 09:25:40 UTC (took 3m 12s)`
  (Russian labels with `language.details: ru`). The check run
  summary of a published review ends with the same line. The values are stored
  in `review_runs.summary["analysis"]`, so both show the same time. No migration.
  The final PAT commit status description leaves the time out so it always
  fits GitHub's 140-character limit; the check run summary keeps it.

- **Review phases in the progress status**: while a review runs, the check run
  summary and the commit status description show the current phase and when it
  started, with a fixed step number, e.g.
  `[2026-10-24 21:20:05 UTC] (4/5) Running model analysis…` (queued,
  fetching PR and diff, reading repository context, running model analysis,
  publishing results). Written once per phase change, best effort, always within
  140 characters. A phase timestamp that stops advancing shows a hung review.
  See [docs/PROGRESS_STATUS.md](docs/PROGRESS_STATUS.md#review-phases).

- **Severity in the transitions table**: the "Previous finding transitions"
  table of the review comment has a Severity column (`Важность` in Russian).
  Findings that are still open (`still_open`, `regressed`, `needs_human`) show a
  traffic light before it: 🔴 P0, 🟠 P1, 🟡 P2, 🟢 P3; fixed findings
  (`resolved`, `obsolete`) show only the severity.
  Rows redacted by the public repository policy keep their severity and light.
  The "Still open from previous review" card headings show the same marker
  before the status, e.g. `[🟠 P1] [regressed] Title`; redacted cards keep it.
  Documented in [docs/PROGRESS_STATUS.md](docs/PROGRESS_STATUS.md#review-comment).

- **Webhook authentication**: `POST /api/v1/pull-request` now requires only a
  valid `X-Hub-Signature-256` HMAC signature; the API key (`REVIEW_API_KEY`) is
  no longer checked on this endpoint. This allows standard GitHub webhooks
  (from repository settings or a GitHub App) to work without custom headers.
- **API key for /api/v1/reviews**: The manual/Actions endpoint continues to
  require `REVIEW_API_KEY` for authentication.
- **Webhook secret validation**: If `GITHUB_WEBHOOK_SECRET` is not configured
  (empty, a placeholder like `change-me`, or shorter than 16 characters), the
  webhook endpoint is disabled (returns 503); `/api/v1/reviews` remains available.
- A startup warning is now logged when `github.allowed_repos` is empty, as this
  configuration accepts webhook requests from any repository.
