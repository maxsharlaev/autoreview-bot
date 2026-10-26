<!-- section: http-api -->
# HTTP API

The API listens on port 8000 (`127.0.0.1:8000` in Compose; put a TLS reverse proxy in front). FastAPI also serves the OpenAPI schema at `GET /openapi.json` and `GET /docs` without authentication.

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `POST /api/v1/reviews` | API key | Start a review (GitHub Actions, scripts, manual). |
| `GET /api/v1/reviews/{review_run_id}` | API key | Status of one run. |
| `GET /api/v1/ops/status` | operator key only | Queue and failure snapshot. |
| `POST /api/v1/pull-request` | webhook signature | GitHub webhook, routed to an owner by repository. |
| `POST /api/v1/pull-request/{owner_id}` | that owner's webhook signature | GitHub webhook for one owner. |
| `GET /health` | none | Liveness: `{"status": "ok"}`. |
| `GET /is-ready` | none | Postgres and Redis reachable: `{"status": "ready"}`. |
| `GET /metrics` | none | Prometheus (API). The worker serves its own on `:9100/metrics`. Keep both internal. |
| `GET /docs`, `GET /openapi.json` | none | Generated API schema. |

<!-- section: authentication -->
## Authentication

Send the key in one of:

- `Authorization: Bearer <key>`
- `X-Api-Key: <key>`
- HTTP Basic (the password is the key)

Keys:

- `REVIEW_API_KEY` is the **operator key**: every owner, every run, and `ops/status`.
- `owners.<id>.api.key_env` (default `OWNER_<ID>_REVIEW_API_KEY`) is a key **scoped to one owner**: it can start reviews only for that owner and read only that owner's runs. It cannot read `ops/status`.

A missing or wrong key is `401 {"detail": "invalid access key"}`. The webhook endpoints take no API key; they verify `X-Hub-Signature-256`. Never put a key in a URL.

<!-- section: choosing-the-owner -->
## Choosing the owner

With several owners ([MULTI_OWNER.md](MULTI_OWNER.md)) a request is routed by repository. To pick the owner explicitly send `X-Review-Owner: <owner-id>` or `?owner=<owner-id>` (case-insensitive, aliases accepted). Both set to different values is `400`. A scoped key always uses its own owner.

<!-- section: post-api-v1-reviews -->
## `POST /api/v1/reviews`

```bash
curl -sS -X POST https://review.example.com/api/v1/reviews \
  -H "Authorization: Bearer $REVIEW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"repository": "example-org/example-repo", "number": 1}'
```

PowerShell:

```powershell
$headers = @{ Authorization = "Bearer $env:REVIEW_API_KEY" }
$body = @{ pull_url = "https://github.com/example-org/example-repo/pull/1" } | ConvertTo-Json
Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/reviews -Headers $headers -ContentType "application/json" -Body $body
```

<!-- section: body -->
### Body

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `pull_url` | string | — | `https://github.com/<owner>/<repo>/pull/<n>`. Either this, or `repository` + `number`. |
| `repository` | string | — | `owner/repo`. |
| `number` | int ≥ 1 | — | PR number. |
| `head_sha` | string | — | The commit you expect to be reviewed. If it is not the PR's current head (full SHA, case-insensitive), the request is rejected with `409 stale_head_sha` before anything changes. |
| `force` | bool | `true` | `true`: start a new run even if one exists for this SHA (an open one is cancelled). `false`: reuse a pending, running or completed run for the same SHA. |
| `report_status` | bool | `true` | `false`: this run reports no progress status. An open status of a replaced run is still closed. |
| `head_ref`, `title`, `html_url`, `body` | string | — | Optional hints; GitHub's values are used for the review. |
| `base_sha` | string | — | Accepted for compatibility and ignored; the current base from GitHub is used. |

The endpoint always reads the PR from GitHub with the owner's credentials and binds the run to its current head. Draft and fork PRs are accepted here but the worker skips them.

<!-- section: response-202 -->
### Response `202`

```json
{"status": "pending", "review_run_id": "<uuid>", "head_sha": "<sha>", "trigger": "manual", "owner": "<owner-id>"}
```

`status` can be `running` or `completed` when `force: false` reused a run.

<!-- section: errors -->
### Errors

| HTTP | `detail` | Meaning |
| --- | --- | --- |
| 400 | text | `X-Review-Owner` and `?owner=` disagree. |
| 401 | `invalid access key` | Key missing or wrong. |
| 403 | `repo_not_allowed` | Repository not in the owner's allowlist. |
| 403 | `unknown_owner` | No owner claims the repository and `routing.unclaimed: reject`. |
| 403 | `owner_disabled` | The owner is disabled (`enabled: false`). |
| 403 | `owner_forbidden` | A scoped key asked for another owner. |
| 404 | `unknown_owner` | The owner given in the header/query does not exist. |
| 404 | `owner_not_configured` | The routed owner has no configuration. |
| 409 | `owner_repo_conflict` | The explicit owner is not the one that claims this repository. |
| 409 | `repo_renamed: …` | GitHub returned a new repository name that routes to another owner. |
| 409 | `{"code": "stale_head_sha", "head_sha": "<requested>", "current_head_sha": "<current>"}` | `head_sha` is not the current head, also when the head moved while the request waited (see below). Nothing was cancelled, stored, queued or reported. |
| 422 | validation error | Need `pull_url`, or `repository` + `number`. |
| 502 | `github: …` | Reading the PR from GitHub failed (credentials, permissions, PR missing, timeout). Nothing was queued. |
| 503 | `{"code": "pr_queue_busy"}` + `Retry-After: 5` | Another request for the same PR held the queue lock for 20 s. Nothing changed; retry. |

If enqueueing to Redis fails, the request fails with `500` and the run is stored as `failed` / `QUEUE_UNAVAILABLE`; the next request replaces it.

<!-- section: concurrency-guarantees -->
### Concurrency guarantees

- Enqueueing is serialized per PR (repository + number) with a Postgres advisory lock that the webhook takes too. A request waits for it at most 20 s, then gets `503 pr_queue_busy`.
- If a manual request would cancel an open run of the same owner on a **different** SHA, the endpoint re-reads the PR head under the lock (at most 10 s). A moved head gives `409 stale_head_sha`; a failed re-read gives `502`.
- Each run's ARQ job id is `review:<run id>` and is stored with the run, so the same run can never be queued twice. A `pending` run younger than 60 s whose job is not in Redis yet still counts as in flight; after that it is treated as lost and replaced.
- The worker only processes `pending` runs and claims them with a conditional `pending → running` update, so of two competing jobs only one reviews.
- A superseded job is aborted after the new run is committed (best effort, at most 2 s).

<!-- section: get-api-v1-reviews-review-run-id -->
## `GET /api/v1/reviews/{review_run_id}`

```json
{
  "review_run_id": "<uuid>",
  "status": "pending | running | completed | failed | skipped | cancelled",
  "trigger": "manual | webhook",
  "head_sha": "<sha>", "base_sha": "<sha>",
  "error_code": null, "skip_reason": null, "duration_ms": 42000,
  "summary": {},
  "owner": "<owner-id>",
  "pull_request": {"repository": "org/repo", "number": 1, "html_url": "…", "issue_key": "ABC-12"}
}
```

`404 review run not found` also for a run of another owner when a scoped key is used. Error and skip codes: [TROUBLESHOOTING.md](TROUBLESHOOTING.md#error-and-skip-codes).

<!-- section: get-api-v1-ops-status -->
## `GET /api/v1/ops/status`

Operator key only. Returns `in_flight` (pending/running counts), `queue_jobs` (ARQ queue depth), `stale_minutes`, `stale_runs`, `last_completed`, `last_failures` and `hints` (a plain-language diagnosis such as missing model quota or a worker that does not consume the queue).

<!-- section: webhooks -->
## Webhooks

Subscribe to the **Pull request** event. Handled actions: `opened`, `synchronize`, `reopened`, `ready_for_review`, and `labeled` with the size-guard override label (that one forces a new run). Setup: [GITHUB_SETUP.md](GITHUB_SETUP.md).

- `POST /api/v1/pull-request` verifies the signature against every owner's secret, then routes by repository. A repository of another owner than the secret's is `403 owner_signature_mismatch`.
- `POST /api/v1/pull-request/{owner_id}` verifies only that owner's secret.

| HTTP | Body | Meaning |
| --- | --- | --- |
| 202 | `{"status": "pending", "review_run_id": "…", "owner": "…"}` | Queued, or an existing run for this SHA reused (its status is returned). |
| 202 | `{"status": "ok", "event": "ping"}` | Ping event. |
| 202 | `{"status": "ok", "event": "unknown"}` | No `X-GitHub-Event` header. |
| 202 | `{"status": "skipped", "reason": "…"}` | `ignored_event`, `ignored_action`, `ignored_label`, `draft_pr`, `fork_pr`, `repo_not_allowed`, `unknown_owner`, `owner_not_configured`, `owner_disabled`. Draft and fork skips also report the commit's progress status as skipped. |
| 401 | `invalid signature` | Missing or wrong `X-Hub-Signature-256`. On a disabled owner's URL a correct signature gets `202 owner_disabled`. |
| 403 | `owner_signature_mismatch` | Signed with another owner's secret. |
| 404 | `unknown_owner` | Unknown owner in the URL. |
| 409 | `owner_repo_conflict` | The URL's owner does not claim this repository. |
| 503 | text | No valid webhook secret is configured (for that owner): the endpoint is disabled. |
| 503 | `{"code": "pr_queue_busy"}` + `Retry-After: 5` | Queue lock busy; GitHub records a failed delivery that you can redeliver. |

Except for the override label, a webhook never cancels a run for the same SHA: it reuses an open or completed run (`force: false`). A new SHA cancels the open run for the previous one.
