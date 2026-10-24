# Review progress status

Webhook-triggered and manual (`POST /api/v1/reviews`) reviews report progress on the PR head commit, so the author can see whether the bot queued the PR, is working, skipped it or broke, without an extra comment. On by default; existing `config.yaml` files need no change.

```yaml
progress_status:
  enabled: true
  mode: auto          # auto | check_run | commit_status | off (auto = check_run for a GitHub App, commit_status for a PAT)
  reaction: false     # add an eyes reaction while a worker reviews the PR
  timeout_seconds: 5  # per GitHub call
```

Keys and ranges: [CONFIGURATION.md](CONFIGURATION.md#progress_status). Every field can be overridden per owner in `owners.<id>.progress_status`, for example `mode: off` for an owner whose credentials lack the permission.

## GitHub App: check run `autoreview`

- `queued` when the request is accepted and the job is queued, `in_progress` when a worker picks it up, then `completed`.
- Conclusions: `success` (no P0–P2 findings), `neutral` (P0–P2 findings), `skipped` (reason in the summary: draft, fork, repository not allowed, author without write access, size guard, owner disabled or changed), `cancelled` (superseded by a newer push, or the head changed during the review), `failure` (bot errors and timeouts only).
- Findings never produce `failure`, so the check can't block merges.
- Needs the App permission **Checks: Read and write**.

## PAT: commit status `autoreview`

- `pending` while queued or running, then `success` (review published or skipped), `failure` (bot error or timeout) or `error` (cancelled).
- One-line description and a `target_url` pointing to the review comment.
- A fine-grained PAT needs **Commit statuses: Read and write**; a classic PAT needs `repo:status` (included in `repo`).

## Reliability

- Each status is written with the run owner's own credentials; `auto` picks the mode per owner from its credential type.
- Everything is best effort: a missing permission, a GitHub error or a slow call (capped by `timeout_seconds`) is logged and ignored and never fails or delays the review. Deployments without the permission keep working, just without a status. Webhook-level skips (draft, fork, repository not allowed) are reported during the webhook request and add at most `timeout_seconds` to the response.
- The worker sets the final state in a `finally` block, including errors and the ARQ job timeout. A worker cron job every 10 minutes marks statuses still open after `WORKER_JOB_TIMEOUT` plus 5 minutes as timed out, so a crashed worker can't leave a status pending forever.
- A new push creates a new check run on the new head and marks the still-open run for the previous SHA `cancelled`.
- Write order: the run row is committed before the job is enqueued. If enqueueing fails (for example Redis is down) the run becomes `failed` / `QUEUE_UNAVAILABLE` and no status is created. The queued status is written after a successful enqueue, with the run row locked and only while the run is still pending, so a fast worker's newer state is never overwritten and only one check run is created.
- If the PR head moved between queueing and pickup, the worker does not review the new code under the old run: it closes the run as `HEAD_CHANGED` (status `cancelled` on the run's own SHA) and never reports on another commit.
- Queue serialization, the 60 s in-flight grace and the conditional `pending → running` claim are described in [API.md](API.md#concurrency-guarantees). Owner-reason skips taken before the claim use the same `pending` condition: if another job already holds the run, nothing is written and its status is left alone. A job that ARQ retries after a worker crash finds its run `running` and is skipped; the next request for that SHA replaces it, and the cleanup job closes the stale status.

## Same commit, two triggers

- At most one status per commit is open at a time.
- A manual run with `force: true` (the default) replaces an open run on the same SHA, including a webhook run: the old run's status is closed as `cancelled` and the new run gets its own.
- A webhook (or `force: false`) on a SHA with an open run reuses that run and its status.
- A worker whose status was already closed by a newer run never writes to it again.
- An open run whose job was lost is closed as `failure` when it is replaced.

## Manual runs

Manual runs report exactly like webhook runs: same owner credentials and mode, write order, skips, cancellation and disclosure rules. The run is bound to the PR's current head; a stale `head_sha` is rejected with `409 stale_head_sha` before anything is reported ([API.md](API.md#errors)). `"report_status": false` starts a run without a status; an open status of the run it replaces is still closed. Request errors (unknown owner, repository not allowed, GitHub unavailable) are HTTP errors and create no status.

Scheduled digest runs report no status: they review no PR head.

## Owners disabled or removed

- **Owner disabled** after the webhook (`owner_disabled`): the worker, or the cleanup job for a stale status, closes the status as `skipped` with that owner's own credentials. The registry keeps a disabled owner's credentials for this purpose only; if they can't be loaded, the status is closed only in the database.
- **Owner removed** from the config (`owner_not_configured`): **its open statuses can't be closed on GitHub.** No credentials exist for it, and the bot never uses another owner's or the global credentials, so the check run stays `queued` / `in_progress` (or the commit status `pending`). The cleanup job closes it in the database and logs a warning. To avoid this, disable the owner first (`enabled: false`), let its queued runs drain, then remove the block; or close the check runs by hand.

## What a status discloses

Summaries contain only the state, the skip reason or error code, finding counts and the comment link. They never include model text, finding details or Jira text. The task key and task alignment are added for private repositories; for public repositories only the key is shown, and only when `public_repos.jira_disclosure` is `key_only` or `full`.

## `has_sensitive_content`

Each run records `comment_url` and `has_sensitive_content`: whether the review comment it published would have contained security finding details or Jira text (unmet acceptance criteria, or any review text written with Jira context in the prompt) before the public repository policy was applied. `NULL` means unknown: runs from before migration `008_review_run_progress` and runs that published no review comment. The flag is groundwork for a later admin view; nothing acts on it yet.
