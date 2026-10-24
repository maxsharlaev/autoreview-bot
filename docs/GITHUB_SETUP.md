# GitHub App, PAT and trigger setup

Full host deployment (Compose, network, Prometheus): [DEPLOY.md](DEPLOY.md). Russian translation: [ru/GITHUB_SETUP.md](ru/GITHUB_SETUP.md).

The service receives Pull request events on `POST /api/v1/pull-request`. That endpoint requires only a valid `X-Hub-Signature-256` (HMAC-SHA256 with `GITHUB_WEBHOOK_SECRET`); no API key, because GitHub webhooks cannot send custom headers. If `GITHUB_WEBHOOK_SECRET` is not configured (empty, a placeholder, or shorter than 16 characters), the webhook endpoint is disabled (`503`) but the service starts and `/api/v1/reviews` keeps working with `REVIEW_API_KEY`. Never put credentials in a webhook URL.

`GET /health`, `GET /is-ready` and `GET /metrics` are open for probes and Prometheus; the worker serves `GET http://worker:9100/metrics`. Queue snapshot: `GET /api/v1/ops/status` (API key). API reference: [API.md](API.md).

## Where secrets go

| Value | Location | Purpose |
| --- | --- | --- |
| `REVIEW_API_KEY` | Server `.env` | Protects the review and operations APIs |
| `AI_REVIEW_API_KEY` | GitHub organization or repository **Secrets** | Same value as `REVIEW_API_KEY` (or an owner's scoped key) |
| `GITHUB_WEBHOOK_SECRET` | GitHub App **or** repository webhook, **and** server `.env` | `X-Hub-Signature-256` HMAC |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | Server `.env` only | App JWT → installation token. Not needed with a PAT |
| `GITHUB_TOKEN` / `GITHUB_PAT` | Server `.env` only | Fine-grained PAT (`github_pat_*`); used instead of the App when set |
| `OPENAI_API_KEY`, Jira token | Server `.env` / host secrets only | Codex and Jira. Not needed in reviewed repositories' secrets |

Generate a long random service key (for example `openssl rand -hex 32`); it is separate from the GitHub PAT. All variables: [ENVIRONMENT.md](ENVIRONMENT.md).

## Option A: fine-grained PAT

Without a GitHub App, a PAT (`github_pat_*`) with access to the reviewed repositories is enough. In `.env`:

```bash
GITHUB_TOKEN=github_pat_...
# or GITHUB_PAT=github_pat_...
REVIEW_API_KEY=<service API key, not the PAT>
GITHUB_WEBHOOK_SECRET=<repository webhook secret, if you use a webhook>
```

Repository permissions:

- **Pull requests**: Read and write (sticky comment, PR title/description).
- **Contents**: access for `git clone`. GitHub often requires **Read and write** for HTTPS clone of a fine-grained PAT, even though the bot only reads.
- **Commit statuses**: Read and write for the `autoreview` progress status (classic PAT: `repo:status`). Optional; without it the review runs without a status.

Leave the App fields (`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`) empty; with a PAT the App JWT is not used. PR comments appear under the token owner's account. Test on one repository first and verify both clone and comment.

## Option B: GitHub App

1. Organization (or account) **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**.
2. Name, for example `Autoreview`. **Homepage URL**: the service URL or internal docs.
3. **Webhook**: either disable it and trigger reviews with GitHub Actions (the App still supplies the token for PR reads, clone and comments), or enable it with `https://review.example.com/api/v1/pull-request` (no key in the URL) and the same `GITHUB_WEBHOOK_SECRET` on the App and the server.
4. **Repository permissions**:
   - **Contents**: Read-only
   - **Pull requests**: Read and write
   - **Metadata**: Read-only
   - **Checks**: Read and write, for the `autoreview` check run (optional; without it the review runs without a status)
5. With a webhook, subscribe only to **Pull request** events.
6. Where can this App be installed: **Only on this account** (or any account if you serve several organizations with one App, see [MULTI_OWNER.md](MULTI_OWNER.md#connecting-another-organization)).
7. Create the App and record the **App ID** → `GITHUB_APP_ID`.
8. **Generate a private key** and put the `.pem` contents (or the file path) into `GITHUB_APP_PRIVATE_KEY`.
9. **Install** the App on the repositories in `github.allowed_repos`. The **Installation ID** can go into `GITHUB_INSTALLATION_ID`; `0` lets the service resolve it.

A direct webhook needs only `GITHUB_WEBHOOK_SECRET`. `AI_REVIEW_API_KEY` is for GitHub Actions (`/api/v1/reviews`) and manual calls.

## Organization or repository secret for Actions

1. Organization **Settings** → **Secrets and variables** → **Actions** → **New organization secret** (or the repository's secrets).
2. Name `AI_REVIEW_API_KEY`, value: the server's `REVIEW_API_KEY` (or the owner's scoped key).
3. Repository access: the repositories that call the API.
4. Variable `AI_REVIEW_URL`: the HTTPS base URL without a trailing slash.

Copy [the example workflow](../examples/github/ai-review-trigger.yml) into each reviewed repository as `.github/workflows/ai-review.yml`. It references `maxsharlaev/autoreview-bot@main`; for a fork or an inaccessible workflow adjust `uses:` or copy the steps of this repository's reusable workflow. It calls `POST /api/v1/reviews` with the PR URL, so no webhook HMAC is needed. This repository's `ci.yml` only tests and builds the service. Filters: [TROUBLESHOOTING.md](TROUBLESHOOTING.md#github-actions-in-a-reviewed-repository).

## Service configuration

`.env` for an App:

```bash
REVIEW_API_KEY=<same as AI_REVIEW_API_KEY>
GITHUB_WEBHOOK_SECRET=<App webhook secret>
GITHUB_APP_ID=<app id>
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----..."
GITHUB_INSTALLATION_ID=0
WORKER_MAX_JOBS=4
```

`config.yaml`:

```yaml
github:
  allowed_repos:
    - example-org/example-repo
    # - example-org/another-repo
```

Events from repositories outside a non-empty list are ignored (`202`, `repo_not_allowed`); manual API calls get `403`.

## Verify

After `docker compose up` and public HTTPS (or a tunnel):

1. Set up the Action as above.
2. Open a non-draft test PR. The worker log shows a `review_run`; the PR gets a sticky comment `<!-- open-pr-review -->`.
3. Run status:

   ```bash
   curl -H "Authorization: Bearer $AI_REVIEW_API_KEY" \
     https://review.example.com/api/v1/reviews/<review_run_id>
   ```

## Parallel PRs

Each run is a separate queue job. The ARQ worker takes up to `WORKER_MAX_JOBS` jobs at once (default **4**); different PRs run in parallel. Repeating the same `head_sha` reuses the run, a new SHA cancels the unfinished one, and concurrent deliveries for one PR are serialized ([API.md](API.md#concurrency-guarantees)).

To scale: raise `WORKER_MAX_JOBS` and/or the number of `worker` containers. The limits are the model quota and cloning, not FastAPI.

## Several owners

Each owner in `owners:` has its own GitHub credentials, webhook secret and optional scoped API key, set by env var name. Naming, the two ways to connect another organization (same App with another installation, or an own App/PAT with `/api/v1/pull-request/<owner-id>`), and the owner-specific responses: [MULTI_OWNER.md](MULTI_OWNER.md).

## Common errors

| Symptom | Check |
| --- | --- |
| `503` on the webhook | `GITHUB_WEBHOOK_SECRET` not configured (empty, placeholder or too short) |
| `401` on the webhook | `X-Hub-Signature-256` missing or wrong: compare the secret on the App and the server |
| `401` on `/api/v1/reviews` | `REVIEW_API_KEY` missing or wrong |
| `repo_not_allowed` | A non-empty `github.allowed_repos` must contain the exact `owner/repo` (or `owner/*`) |
| Webhook `202`, no comment | Worker running; repository allowed; PR not draft/fork; author has write access ([TROUBLESHOOTING.md](TROUBLESHOOTING.md)) |
| Clone or comment `403` | Clone and REST are different: a fine-grained PAT often needs Contents **Read and write** for clone; commenting needs Pull requests **write** |
| No progress status | Checks (App) or Commit statuses (PAT) permission ([PROGRESS_STATUS.md](PROGRESS_STATUS.md)) |
