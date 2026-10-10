# GitHub App, PAT and trigger setup

See [deployment](DEPLOY.md), [operations](OPERATIONS.md) and the [Russian translation](ru/GITHUB_SETUP.md).

`POST /api/v1/pull-request` requires only a valid `X-Hub-Signature-256` HMAC-SHA256 signature computed with `GITHUB_WEBHOOK_SECRET`; no API key is needed (GitHub webhooks cannot send custom headers). If `GITHUB_WEBHOOK_SECRET` is not configured (empty, a placeholder, or shorter than 16 characters), the webhook endpoint is disabled (returns 503) but the service starts and `/api/v1/reviews` remains available for Actions-only deployments. `POST /api/v1/reviews` requires `REVIEW_API_KEY`. Do not place credentials in a webhook URL.

## Secrets

| Value | Location | Purpose |
| --- | --- | --- |
| `REVIEW_API_KEY` | Server `.env` | Protects review and operations APIs |
| `AI_REVIEW_API_KEY` | GitHub Actions secret | Same value as `REVIEW_API_KEY` |
| `GITHUB_TOKEN` / `GITHUB_PAT` | Server `.env` only | Optional fine-grained PAT, used instead of App credentials |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | Server `.env` only | App JWT and installation token |
| `GITHUB_WEBHOOK_SECRET` | App/webhook and server `.env` | Webhook HMAC secret |
| `OPENAI_API_KEY`, Jira token | Server `.env` only | Reviewer and issue access |

Generate a long random service API key, for example `openssl rand -hex 32`. It is separate from the GitHub PAT.

## Option A: fine-grained PAT

Give the PAT access to every allowed repository, with Pull requests read/write and Contents access for clone. Set `GITHUB_TOKEN=github_pat_...` in `.env` and leave App fields blank. PR comments appear under the token owner's account. Use a test repository first and verify clone plus comment permissions.

## Option B: GitHub App

1. Create a GitHub App under organization or account developer settings. Give it repository permissions: Contents read, Pull requests read/write and Metadata read.
2. For the current Actions-based MVP, the App can have webhooks disabled. The App still supplies a token for PR reads, clone and comments.
3. Record the App ID as `GITHUB_APP_ID`; generate a private key and place its contents or file path in `GITHUB_APP_PRIVATE_KEY`. `GITHUB_INSTALLATION_ID=0` lets the service resolve the installation.
4. Install the App on each repository listed in `config.yaml` under `github.allowed_repos`.
5. Subscribe to Pull request events and use `https://review.example.com/api/v1/pull-request`. Configure `GITHUB_WEBHOOK_SECRET` on both the App and the service—webhook signature verification is required.

## Trigger reviews with Actions

Copy [the example workflow](../examples/github/ai-review-trigger.yml) into each code repository as `.github/workflows/ai-review.yml`. Add `AI_REVIEW_API_KEY` as a secret and `AI_REVIEW_URL` as the HTTPS base URL without a trailing slash. The example references `maxsharlaev/autoreview-bot@main`; for a fork or inaccessible workflow, adjust `uses:` or copy the steps from this repository's reusable workflow. This repository's `ci.yml` only tests/builds this service; it does not request reviews.

The workflow calls manual `POST /api/v1/reviews` with a PR URL. It does not need a webhook HMAC. The reviewed repository must be on the allowlist; draft and fork PRs are skipped. See [operations](OPERATIONS.md) for branch/path filters.

## Several owners

Each owner in the `owners:` block of `config.yaml` has its own GitHub credentials and webhook secret, set by env var name (see the [README](../README.md#multiple-owners)). Without explicit `*_env` names the variables are `OWNER_<ID>_GITHUB_TOKEN` (PAT) or `OWNER_<ID>_GITHUB_APP_ID` and `OWNER_<ID>_GITHUB_APP_PRIVATE_KEY` (App), `OWNER_<ID>_GITHUB_WEBHOOK_SECRET` and `OWNER_<ID>_REVIEW_API_KEY`, with `<ID>` upper-cased and `-` replaced by `_`.

Two ways to connect another organization:

1. **Same GitHub App, another installation.** Install the App in the other organization. Give the owner the same App env names (and webhook secret) and claim its repositories with `allowed_repos: [org-b/*]` or set its `installation_id`. Events keep going to `/api/v1/pull-request`; the service routes them by repository and installation id. A shared secret is allowed here.
2. **Own App or PAT.** Create a separate App or PAT for the owner and point its webhook at `https://review.example.com/api/v1/pull-request/<owner-id>`. That URL verifies only this owner's secret.

For Actions triggers, use the operator `REVIEW_API_KEY` with `X-Review-Owner: <owner-id>` (or `?owner=`), or give the repository the owner's scoped key from `api.key_env`. Additional responses:

| Response | Meaning |
| --- | --- |
| `403 owner_signature_mismatch` | The webhook was signed with another owner's secret than the owner the repository routes to |
| `202 skipped unknown_owner` / API `403` | No owner claims the repository and `routing.unclaimed: reject` |
| `404 unknown_owner` | Unknown owner in the URL, `X-Review-Owner` or `?owner=` |
| `409 owner_repo_conflict` | An explicit owner was given for a repository claimed by another owner |
| `403 owner_forbidden` | A scoped API key was used for another owner |
| `401` on `/pull-request/<id>` of a disabled owner | Unsigned or wrongly signed; a correct signature gets `202 owner_disabled` |

## Verify and troubleshoot

Start the service, open a non-draft test PR, and check worker logs for a `review_run` and the PR for `<!-- open-pr-review -->`. Query the run with `Authorization: Bearer <REVIEW_API_KEY>` on `/api/v1/reviews/{review_run_id}`.

| Symptom | Check |
| --- | --- |
| `503` on webhook | `GITHUB_WEBHOOK_SECRET` not configured (empty, placeholder, or too short) |
| `401` on webhook | `X-Hub-Signature-256` missing or invalid (check `GITHUB_WEBHOOK_SECRET` match) |
| `401` on `/api/v1/reviews` | `REVIEW_API_KEY` missing or invalid |
| `repo_not_allowed` | Non-empty `github.allowed_repos` contains exact `owner/repo` |
| API `202`, no comment | Worker, queue, draft/fork status, author access and logs |
| Clone or comment `403` | PAT/App installation and Contents/PR permissions |

Sequential deliveries for the same SHA can reuse a run. A new SHA requests cancellation of the previous job. Concurrent delivery protection and a final SHA check are [planned](ROADMAP.md).
