# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Fixed
- **ARB34-009:** revoke scheduler returns accepted/rejected, dedupes by `job_id`, retries transient failures with exponential backoff until max token TTL, and keeps a bounded in-memory retry set; reaper schedules before delete and retains container/checkout when the queue rejects (no persistent tombstones — restart worst case is token TTL).
- **ARB34-008:** launcher reaper schedules proxy revokes via the bounded revoke scheduler (wall-clock timeout) instead of inline `revoke_job_token_sync`, so a slow-dripping proxy cannot stall container/checkout cleanup.
- **ARB34-006 (follow-up):** container-runner Compose no longer sets `OPENAI_API_KEY` / `PROXY_OWNER_KEY_ENVS` in `environment:` (those overrode `model-proxy.env`); secrets come from `model-proxy.env` only.
- **ARB34-005:** launcher proxy revokes use a dedicated asyncio loop with `asyncio.timeout` and a bounded pending queue; `_CANCEL_POOL` is Docker-cancel only so blocked revokes cannot starve DELETE cancel.
- **ARB34-006:** model-proxy loads `model-proxy.env` via `env_file` (gitignored; example + restricted-perms docs) so env vars named in `PROXY_OWNER_KEY_ENVS` reach the proxy; worker/launcher must not mount that file.
- **ARB34-007:** no rolling mix for worker+launcher+model-proxy — deploy/upgrade docs (EN/RU); launcher returns **426** `CODEX_UPGRADE_REQUIRED` when `proxy_token` is missing or legacy `X-Model-Api-Key` is present (never forwarded); worker surfaces it clearly (and maps bare 422 to the same upgrade code).
- **ARB34-004:** `ContainerReviewExecutor.run` bounds finish-path `revoke_job_token` with `asyncio.timeout` / `OPR_PROXY_FINISH_REVOKE_TIMEOUT_SECONDS` (default 2s) so a slow proxy cannot delay lease cleanup or returning a completed review; reaper/launcher revoke remain backstops.
- **ARB34-003:** replace hyphen-canonicalization with explicit `PROXY_OWNER_KEY_ENVS` (owner_id → env var *name*; secrets stay in env). Startup fails on duplicate env targets, missing/empty referenced env, or case-variant owners sharing one env; legacy `PROXY_OWNER_<UPPER>_OPENAI_API_KEY` remains only for owner ids without `-`.
- **ARB34-002:** launcher cancel revokes the proxy token as a best-effort background task on the long-lived cancel pool (short timeout) after the bounded DELETE response, so a slow/unreachable model-proxy cannot delay cancel past the worker's 5s read timeout; reaper revoke remains the idempotent backstop.
- **AR-030-001:** Launcher cancel Docker calls (`list`/`kill`/`remove`) enforce a wall-clock deadline via ``asyncio.timeout`` around ``httpx.AsyncClient`` (including body read), not HTTPX per-phase inactivity timeouts alone. Trickled daemon responses can no longer outlive ``LAUNCHER_CANCEL_DEADLINE_SECONDS``; on deadline cancel returns ``confirmed=false`` so the worker keeps staging. Transport-level tests cover trickle and slow-headers against a real local HTTP server.

### Added
- **M3 PR3 container↔model-proxy wiring:** worker issues per-job `opr_job_` tokens (per-owner upstream key) and passes them to the launcher as `proxy_token` (no `X-Model-Api-Key`); launcher injects only `CODEX_API_KEY=<job token>` and seeds `/work/codex-config.toml` with custom `opr_proxy` provider; review containers stay on internal `review_egress` (no internet) while model-proxy reaches upstream via `proxy_egress`; tokens revoked on finish, cancel, and reaper (idempotent). `in_process` unchanged.
- **ARB32-009:** model-proxy header and chunk waits share one cancellation-safe `_race_primary_or_invalidated` helper whose `finally` always cancels and awaits unfinished tasks and `aclose`s any late upstream response, so timeout / revoke / expiry / client cancel cannot leave a paid upstream request running.
- **ARB32-006 / ARB32-007 / ARB32-008:** per-token invalidation signal (``threading.Event`` set on revoke/expiry) wakes upstream chunk waits even when the upstream is silent; non-stream invalidation discards any buffered body and returns 401; catch_all keeps slot ownership until ``forward_responses`` returns (StreamingResponse transfers to stream ``finally``) so cancellation frees the slot exactly once.
- **ARB32-005 / ARB32-006:** model-proxy reserves the per-job concurrency/request slot before reading the client body and refunds it on upload failure/disconnect/413/timeout; client `request.stream()` waits use the same wall-clock idle/overall bounds as upstream. Upstream chunk waits are also capped by remaining job-token lifetime and re-check `still_valid` after each chunk before forwarding (revoke/expiry mid-wait closes upstream immediately).
- **ARB32-001 / ARB32-002 / ARB32-003 / ARB32-004:** model-proxy rejects oversized ``Content-Length`` with 413 before read and bounds chunked bodies via incremental ``request.stream()``; enforces ``PROXY_MAX_BYTES_PER_JOB`` on outbound bytes incrementally/atomically (stream + non-stream, concurrent-safe) and closes upstream at cap; wall-clock ``PROXY_STREAM_IDLE_TIMEOUT_SECONDS`` per chunk wait and ``PROXY_UPSTREAM_TIMEOUT_SECONDS`` overall deadline via ``asyncio.timeout`` (HTTPX float timeouts are soft backstops only; also stops at job-token expiry); sends ``Accept-Encoding: identity`` upstream and strips ``content-encoding`` / ``content-length`` / ``transfer-encoding`` from forwarded responses.
- **M3 PR2 model-proxy service:** new `model-proxy` service (`app/model_proxy`) in the container-runner Compose overlay (internal + `review_egress`, no Docker socket). Worker issues short-lived hashed `opr_job_` tokens (`POST/DELETE /internal/jobs/{job_id}/token` with `PROXY_ISSUER_TOKEN`); public allowlist is only `POST /v1/responses` with SSE passthrough, fixed upstream host, no CONNECT/WebSocket/Host override. Per-owner upstream keys (`PROXY_OWNER_<ID>_OPENAI_API_KEY`), body/request/byte/concurrency limits, Prometheus `/metrics`, `/healthz`. Review-container wiring stays for a follow-up PR; `in_process` unchanged.
- **AR-030-002:** Cancel-path Docker calls use the DockerClient's async client factory (injectable, or derived from an injected sync client's base_url/MockTransport) so deadline-bound list/kill/remove hit the same endpoint/transport as the configured client instead of always rebuilding from ``DOCKER_HOST``.
- **AR-030-003:** `_remaining_cancel_budget` never returns more than the actual remaining time (sub-50ms remainder skips the Docker call → `confirmed=false`). Cancel deadline validation requires a mandatory 0.5s margin below the worker DELETE read timeout (max 4.5s; default 4s still valid).
- **AR-030-004:** DELETE cancel is bounded at the HTTP layer: ``cancel_review_job`` runs in a long-lived thread pool; the handler waits at most ``LAUNCHER_CANCEL_DEADLINE_SECONDS`` then returns ``confirmed=false`` while cleanup continues (tombstone set first; in-flight cancels coalesce per job). Docker deadline calls use a long-lived asyncio loop instead of per-call ``asyncio.run`` (avoids blocking on stuck DNS resolver executor shutdown).
- **AR-030-005:** ``cancel_scheduler._submit_cancel`` registers ``add_done_callback`` only after releasing ``_INFLIGHT_LOCK``, so an already-done future cannot deadlock on synchronous ``_clear_inflight`` re-entry into the non-reentrant lock.
- **Issue #29:** Launcher DELETE cancel uses one overall deadline (``LAUNCHER_CANCEL_DEADLINE_SECONDS``, default 4s) below the worker DELETE read timeout; each Docker list/kill/remove call receives only the remaining budget (no 60s client hang).
- **ARB-27-024 / ARB-27-025:** Checkout/job leases use a sidecar ``<dirname>.lease`` beside the directory (not inside the git worktree), so repos that track a top-level ``.lease`` can check out cleanly; reaper removes the sidecar with the dir. ``clone_head`` wraps askpass/git/neutralize in one ``BaseException`` cleanup that stops the lease and removes dir+sidecar.
- **ARB-27-021 / ARB-27-022 / ARB-27-023:** PR language verify treats exhausted JobDeadline as unverified (do not publish; budget notice). Container exit 44 (entrypoint timeout) maps to `CODEX_TIMEOUT` through launcher and executor. In-process executor runs `neutralize_escaping_symlinks` via `asyncio.to_thread`.
- **ARB-27-017 / ARB-27-018 / ARB-27-019 / ARB-27-020:** Staging and other lease-held FS work (safe_copytree, rmtree, symlink neutralization) run via `asyncio.to_thread` so the lease heartbeat can refresh. `clone_head` clamps to remaining `JobDeadline` (minus reserved min Codex call + overhead); PR description skips with the budget notice when clone+call cannot fit. Worker startup requires `codex.timeout_seconds` <= `OPR_MAX_REVIEW_TIMEOUT_SECONDS` for container backend. Container-runner compose lists every supported launcher setting with safe interpolated defaults (docs table cross-checked); still no secrets except `LAUNCHER_TOKEN`.
- **ARB-27-012 / ARB-27-014 / ARB-27-015 / ARB-27-016:** Post-publish budget notices refresh the sticky comment (same upsert path, public redaction preserved). Launcher cancel kills containers immediately then waits (``LAUNCHER_CANCEL_WAIT_SECONDS`` < worker DELETE read timeout). Checkout/job dirs use ``.lease`` heartbeats; the reaper removes only stale leases (no-lease dirs use a conservative age bound). Container orphan TTL must cover max review + wait grace; active lifecycle launches are never reaped.
- **ARB-27-010 / ARB-27-011 / ARB-27-012 / ARB-27-013:** ARQ job deadline clamps each Codex call to remaining `worker_job_timeout` (container clamp subtracts full launcher pipeline overhead so the HTTP deadline fits; skip retry/extras when budget is insufficient). Description-budget skip is decided before sticky publish so the note appears on the comment. `OPR_CLONE_TIMEOUT_SECONDS` is enforced in `git_clone` (process-group kill). Launcher `DELETE /v1/jobs/{job_id}` uses a per-job lifecycle registry (tombstones, wait for in-flight launch) and returns `confirmed: true` only when idle; ContainerReviewExecutor removes staging only when confirmed.
- **ARB-27-008 / ARB-27-009:** API no longer requires `LAUNCHER_TOKEN` at startup (worker + launcher only). Worker clone dirs use strict `ai-review-<uuid4 hex>` names; launcher reaper reclaims them with the same orphan TTL. Orphan TTL bound includes `OPR_CLONE_TIMEOUT_SECONDS` (default 300) so mtime reaping cannot remove an active clone.
- **Checkout orphan TTL bound:** `OPR_CHECKOUT_ORPHAN_TTL_SECONDS` must be >= max review timeout (`OPR_MAX_REVIEW_TIMEOUT_SECONDS` / `codex.timeout_seconds`) + launcher overhead; default TTL is derived from that bound. Launcher and worker fail closed at startup; launcher also rejects per-job timeouts that exceed the TTL.
- **ARB-27-006 / ARB-27-007:** container executor HTTP read timeout follows `codex.timeout_seconds` + launcher overhead; startup fails if that exceeds `worker_job_timeout`. Launcher reaper removes orphan UUID dirs under `OPR_CHECKOUT_BASE` (`OPR_CHECKOUT_ORPHAN_TTL_SECONDS`) without following symlinks.
- **ARB-27-005:** review containers set `apparmor=` from `LAUNCHER_APPARMOR_PROFILE` (default `unconfined`) so bubblewrap works on AppArmor Linux hosts; dedicated profile remains planned.
- **ARB-27-004 / ARB-27-003:** launcher no longer loads `env_file: .env` (only `LAUNCHER_TOKEN` + explicit non-secret config); Prometheus joins `internal` as well as `default` so it can scrape api/worker.
- **ARB-27-002 / ARB-27-001:** checkout staging never dereferences escaping symlinks (`safe_copytree` / neutralize); launcher mounts `opr_checkouts` via volume Subpath (Engine ≥ 26, fail-closed at startup) instead of launcher-local binds.
- Narrow seccomp profile for review containers (`docker/review/seccomp/review-bwrap.json`) instead of `seccomp=unconfined`; `scripts/stack.ps1` / `scripts/stack.sh` for compose overlays.
- **M2 container review backend** (launcher via opt-in `docker-compose.container-runner.yml`; default Compose/CI unchanged): optional `codex.runner_backend: container` runs each Codex review in an ephemeral container via a dedicated **launcher** service (the only holder of the Docker socket). Hard-coded create spec: RO checkout, cap_drop ALL, no-new-privileges, resource limits, `opr.review=1` label, always remove + orphan reaper. Model key via env into the review process only (M3 will replace with a model-API proxy). Fail closed (`CODEX_SANDBOX`) with no fallback to `in_process`. Default remains `in_process`.


## [Unreleased]

### Security

- **AR-003 sandbox mode gate:** when `codex.allow_unsandboxed` is `false` (default),
  only `codex.sandbox: read-only` is accepted at config load; `workspace-write` and
  `danger-full-access` fail startup with a clear error. The Linux sandbox preflight
  uses the same `sandbox_mode` as `codex exec`. Escape hatch: `allow_unsandboxed: true`.

- **Execpolicy denylist only:** review-agent rules no longer include `allow` prefixes;
  they are a best-effort forbid list. Containment remains no secrets in child env +
  read-only FS + no network + PID namespace + fail-closed (see SECURITY.md).

- **Review-agent execution policy + sandbox fail-closed:** each Codex run gets a dedicated `CODEX_HOME` with `shell_environment_policy` (`inherit = "core"`, default `KEY`/`SECRET`/`TOKEN` excludes; **PATH is not set in policy** so the `codex-linux-sandbox` helper stays resolvable) so agent-spawned processes do not inherit the model key, plus execpolicy denylist (forbid-only) rules for interpreters, package managers, network tools, build/test runners and known bypass prefixes. AR-001/AR-002-class gaps are **accepted platform limitations** for public repos (no PATH wrappers / `noexec`). Verified on Codex 0.164.0: sandboxed commands get a fresh PID namespace; planted worker canary and worker secret names are not visible via `/proc/*/environ` inside the sandbox. Reviews **fail closed** with `CODEX_SANDBOX` when the Linux sandbox cannot start (userns / helper); `codex.allow_unsandboxed` defaults to `false` (trusted-dev hatch only). Strict path on the roadmap: ephemeral per-review container without secrets. See [SECURITY.md](SECURITY.md). Worker image pins `@openai/codex@0.164.0`.


- **Dashboard / alerts:** wrap global gauges (`arq_queue_jobs`, `review_in_flight`,
  `stale_in_flight`, `recent_failures`) in `max(...)` so Grafana stats and alerts
  show one value when the API and every worker export the same series.
### Added

- **ReviewExecutor (M1):** `codex.runner_backend` selects how Codex runs. Only `in_process` is implemented (delegates to the existing `run_codex` path with no behavior change). `container` is rejected at config load until the dedicated launcher backend ships. See [ARCHITECTURE.md](docs/ARCHITECTURE.md#review-executor-backends).

- **Dev Prometheus**: opt-in `docker-compose.observability.yml` runs a local
  Prometheus on the stack's network with the bundled scrape jobs and alert
  rules (UI on `127.0.0.1:9090`, no Alertmanager, tmpfs storage). See
  `docs/OBSERVABILITY.md`.
- **Dev Loki**: the observability overlay also runs Loki (no host port, 512m
  tmpfs, 24h retention via the compactor) and Alloy, which ships this Compose
  project's container logs to it through a pinned `wollomatic/socket-proxy`
  (GET list/inspect/logs/networks only; no archive/export/create/exec) with a
  `service` label. Grafana gets a `loki` datasource, so the dashboard's logs
  panel has data.
- **Dev Grafana**: the observability overlay also runs Grafana on
  `127.0.0.1:3000` with a provisioned Prometheus datasource and the bundled
  dashboard as the home page, read-only (anonymous viewer, local-only `admin`
  login from `GRAFANA_ADMIN_PASSWORD`, default `admin`; edit the JSON in the
  repository for permanent changes).

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
  acceptance criteria in the repository), and control over PR title and
  description edits (policy with per-owner and per-repository overrides,
  deterministic low-quality checks, a two-pass spec check), in
  `docs/ROADMAP.md` and its Russian mirror.

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

- **Worker metrics port**: Compose no longer publishes the worker's `:9100` on
  the host; it is exposed on the Docker network only, so
  `docker compose up --scale worker=N` works. The bundled Prometheus job finds
  every replica through `dns_sd_configs` on the `worker` service name. If you
  scraped `127.0.0.1:9100`, switch to the Docker network (see
  `docs/UPGRADE.md`).
  The worker alert is split: `OpenPrReviewWorkerDown` (`page`) fires when no
  worker replica is discovered (`absent(up{job="open-pr-review-worker"})`) or
  every known worker target is down (`max(...) == 0`; after all workers stop,
  Prometheus keeps the last DNS-discovered target at `up == 0`), and
  the new `OpenPrReviewWorkerTargetDown` (`warning`) fires when a discovered
  replica cannot be scraped (`up == 0`). Neither detects losing only some
  replicas (a stopped replica leaves DNS discovery); see
  `docs/OBSERVABILITY.md`.

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
