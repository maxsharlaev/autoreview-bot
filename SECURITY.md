# Security policy

## Supported versions

Only the `main` branch receives security fixes. There are no versioned releases yet.

| Branch | Supported |
| --- | --- |
| `main` | Yes |

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion for a vulnerability.**

Report it privately through [GitHub Security Advisories](https://github.com/maxsharlaev/autoreview-bot/security/advisories/new).

<!-- TODO(Max): add a security contact e-mail as an alternative channel, or confirm that GitHub private advisories are the only channel. -->

Please include a description, steps to reproduce, the impact you expect, and the affected commit. You can expect a first answer within 7 days. Please give the maintainer reasonable time to ship a fix before any public disclosure.

## Threat model and trust boundaries

| Component | Trust |
| --- | --- |
| Operator (server `.env`, `config.yaml`, prompts) | Trusted |
| Owners in `owners:` | Trusted for their own repositories only; isolated from each other |
| Callers of `/api/v1/reviews` | Trusted to the scope of their API key |
| Webhook senders | Trusted only after signature verification |
| **PR content** (title, body, branch, commits, diffs, files of the checkout), **Jira issue text**, earlier findings | **Untrusted** |
| Model output | Untrusted until validated |

What the service does about untrusted content:

- **It never builds, installs or runs PR code.** The worker clones the PR head and runs the Codex CLI in that checkout with `--sandbox read-only` and `--ask-for-approval never`. The sandbox environment contains only the model key and a short list of system variables; GitHub, Jira, Slack and database credentials are not passed ([ENVIRONMENT.md](docs/ENVIRONMENT.md)).
- Inside the sandbox, reads in the checkout run automatically; writes, network access and escaping the sandbox are rejected, and with `never` there is nobody to approve a prompt. The worker container relaxes Docker seccomp/AppArmor only so Codex's inner bubblewrap sandbox can create user namespaces; Codex does not run with `--yolo`.
- PR content is never interpolated into shell commands. Git clones use `GIT_ASKPASS` (user `x-access-token`, password the token); the token is not kept in the remote URL after checkout.
- **Prompt injection.** PR and Jira text is placed inside `<untrusted_…>` blocks, boundary tags inside it are stripped, and the prompts tell the model to treat it as data. Injection can't be ruled out, so the model's output has no privileges: it must match a JSON schema, findings are normalized (unknown severities become P2, findings for paths outside the PR's changed files and earlier findings are dropped, the count is capped), and the only thing published is a comment, title/description and status on that PR.
- **What is published.** Sticky PR comment, optional PR title/description, progress check run or commit status, optional Jira comment/transition and Slack digest. The bot does not push code, merge, approve or change repository settings.
- **Reviewed PRs.** Drafts, forks and authors without write access are skipped by default, and only allowlisted repositories are reviewed.

## Credentials and minimal permissions

| Mode | Permissions |
| --- | --- |
| GitHub App | Contents: Read, Pull requests: Read and write, Metadata: Read, Checks: Read and write (optional, progress status). Subscribe to Pull request events only if you use webhooks. |
| Fine-grained PAT | Only the reviewed repositories. Pull requests: Read and write, Contents (clone; GitHub may require Read and write for clone), Commit statuses: Read and write (optional). Prefer an App: a PAT acts as a person. |
| Actions trigger | `AI_REVIEW_API_KEY` as a repository/org secret. Prefer an owner's scoped key over the operator key. |
| Model key | A dedicated project key with a spending limit. |
| Jira | A service account with browse, comment and transition on the listed projects only. The service never creates issues. |
| Slack | A bot token limited to posting in one private channel. |

Details: [GITHUB_SETUP.md](docs/GITHUB_SETUP.md).

Secret handling:

- Keep secrets in `.env` (git-ignored) or a secrets manager; `config.yaml` names env variables (`*_env`) and never holds secret values.
- Each owner's secrets are `OWNER_<ID>_*` variables ([ENVIRONMENT.md](docs/ENVIRONMENT.md#per-owner-variables-owner_id_)). An owner's run uses only that owner's credentials; it never falls back to the global ones or another owner's.
- Owner credential objects hide secret values in their representation, and secrets are not written to logs on purpose. If a secret appears in a log, treat it as exposed and rotate it.
- Rotate keys after any suspected exposure, and when people with server access leave.

## Request authentication and owner isolation

- **Webhooks** (`POST /api/v1/pull-request[/<owner>]`) require a valid `X-Hub-Signature-256` HMAC-SHA256, compared in constant time. A secret that is empty, a placeholder or shorter than 16 characters disables the endpoint (`503`). The per-owner URL accepts only that owner's secret, and a delivery signed by one owner for a repository routed to another is rejected (`403 owner_signature_mismatch`).
- **API keys** are compared in constant time. The operator `REVIEW_API_KEY` can act for any owner; an owner's scoped key only for its own owner (`403 owner_forbidden`). `/api/v1/ops/status` needs the operator key. Don't put keys in URLs.
- **Owner isolation.** Repositories are claimed by exactly one owner; runs, findings and comment identities carry an `owner_id`. A run whose owner was removed or disabled, or whose repository now routes elsewhere, is skipped instead of being published with other credentials.

Codes and responses: [API.md](docs/API.md), [MULTI_OWNER.md](docs/MULTI_OWNER.md).

## Public repositories

For public repositories the bot redacts security-relevant findings (`public_repos.security_findings: redact` by default, `redact_all` for strict setups), limits Jira disclosure (`jira_disclosure`), and never publishes details recorded while the repository was private. A run that redacted something sets `has_sensitive_content`. See [PUBLIC_DISCLOSURE.md](docs/PUBLIC_DISCLOSURE.md) and [DATA_HANDLING.md](docs/DATA_HANDLING.md).

## Deployment hardening checklist

- [ ] TLS at a reverse proxy; the API is not exposed over plain HTTP.
- [ ] Postgres, Redis and the metrics ports (`:8000/metrics`, `:9100`) are not reachable from the internet.
- [ ] `/docs` and `/openapi.json` are unauthenticated: block them at the proxy if you don't want the API schema public.
- [ ] `REVIEW_API_KEY` and webhook secrets are long random values (`openssl rand -hex 32`).
- [ ] Actions use owner-scoped keys; the operator key stays on the server.
- [ ] `github.allowed_repos` (or `owners[].allowed_repos`) is set; `routing.unclaimed: reject` when several owners are configured.
- [ ] GitHub App instead of a PAT, with only the permissions above.
- [ ] Model key with a spending limit; Jira/Slack service accounts with minimal access.
- [ ] `public_repos` settings reviewed before enabling the bot on a public repository.
- [ ] Logs go to a store with access control and retention.
- [ ] Postgres backups are encrypted and access-controlled (findings can quote code).
- [ ] The service runs from a current `main`.

Deployment: [DEPLOY.md](docs/DEPLOY.md).
