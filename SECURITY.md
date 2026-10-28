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

- **PR code is not built or installed.** The worker clones the PR head and runs the Codex CLI in that checkout with `--sandbox read-only` and `--ask-for-approval never`. Each run gets a dedicated `CODEX_HOME` with (1) `shell_environment_policy` (`inherit = "core"`, default excludes for names containing `KEY`/`SECRET`/`TOKEN`, and a small `set` for `LANG`/`TERM` only — **PATH is not set in policy**, because overriding it drops Codex's `codex-linux-sandbox` helper prefix and breaks bubblewrap) so processes the agent starts do not inherit the model key, and (2) execpolicy prefix rules that allow a small read-only set (`rg`, `cat`, `ls`, `head`, `git diff`/`log`/`show`/`status`) and forbid interpreters, package managers, network tools, opaque shells, build/test runners, and known bypass prefixes (for example preprocessor hooks on `rg`, expression forms on `sed`, `find`, and `git -c` / `--config`). Codex's own process still receives the model key to call the API; only agent-spawned commands are filtered. Give each deployment, or each owner, a dedicated model key with a spending limit. The sandbox environment contains only the model key and a short list of system variables; GitHub, Jira, Slack and database credentials are not passed ([ENVIRONMENT.md](docs/ENVIRONMENT.md)). Secret scanning of model output before publish remains a [planned follow-up](docs/ROADMAP.md#planned-follow-ups).
- **Accepted platform limitations (AR-001 / AR-002 class).** If you run this bot on **public** repositories, you accept these residual Codex platform limits: there is **no default-deny** for unmatched commands that stay *inside* the read-only sandbox, and execpolicy is best-effort (mainly an outside-sandbox escalation gate). We do **not** add PATH wrappers or `noexec` mounts on top of Codex. **Actual containment today:** (1) worker secrets never enter the Codex process (`sandbox_env` allowlist); (2) agent children get `inherit=core` + KEY/SECRET/TOKEN excludes (no model key in child env); (3) Linux bubblewrap with a fresh PID namespace / `/proc` for sandboxed commands (verified: a planted worker canary and worker secret names are not visible via `/proc/*/environ` inside the sandbox); (4) read-only filesystem + no network; (5) **fail closed** when the sandbox cannot start (userns unavailable, missing sandbox helper) — public-facing reviews refuse with `CODEX_SANDBOX` in the bot comment / Check Run unless `codex.allow_unsandboxed` is explicitly set (default `false`, trusted-dev escape hatch only). Per-run `CODEX_HOME` is created under `/var/tmp` (not `/tmp`): Codex refuses sandbox helper aliases under temporary directories, which would break bubblewrap. Residual risk: unmatched in-sandbox tools can still read the checkout and poison model context; a determined agent could still attempt local reads the RO sandbox allows. Codex does not run with `--yolo`.
- **Strict path (roadmap):** ephemeral per-review container without secrets — replace the former “Enforced execution policy / wrappers” follow-up; see [ROADMAP.md](docs/ROADMAP.md#planned-follow-ups).
- PR content is never interpolated into shell commands. Git clones use `GIT_ASKPASS` (user `x-access-token`, password the token); the token is not kept in the remote URL after checkout.
- **Prompt injection.** PR and Jira text is placed inside `<untrusted_…>` blocks, boundary tags inside it are stripped, and the prompts tell the model to treat it as data. Injection can't be ruled out, so the model's output is constrained: it must match a JSON schema, and findings are normalized (unknown severities become P2, findings for paths outside the PR's changed files and earlier findings are dropped, the count is capped). Its effects are limited to the outputs listed below. Note that P0/P1 findings come from the model, so when `features.jira_transition` is on, model output can move the linked Jira issue to `rework_status`, and with `features.jira_comment` it can comment on it. Turn these off, or limit `jira.projects`, if that is not acceptable.
- **What is published.** Sticky PR comment, optional PR title/description, progress check run or commit status, optional Jira comment/transition and Slack digest. The bot does not push code, merge, approve or change repository settings.
- **Reviewed PRs.** Drafts, forks and authors without write access are skipped by default, and only allowlisted repositories are reviewed **when an allowlist is set**. An empty `github.allowed_repos` (or owner `allowed_repos`) accepts every repository the credentials can reach, with a startup warning; set it explicitly.

## Credentials and minimal permissions

| Mode | Permissions |
| --- | --- |
| GitHub App | Contents: Read, Pull requests: Read and write, Metadata: Read, Checks: Read and write (optional, progress status). Subscribe to Pull request events only if you use webhooks. |
| Fine-grained PAT | Only the reviewed repositories. Pull requests: Read and write, Contents: Read (clone), Commit statuses: Read and write (optional). Prefer an App: a PAT acts as a person. |
| Actions trigger | `AI_REVIEW_API_KEY` as a repository/org secret. Prefer an owner's scoped key over the operator key. |
| Model key | A dedicated project key with a spending limit. |
| Jira | A service account with browse, comment and transition on the listed projects only. The service never creates issues. |
| Slack | A bot token limited to posting in one private channel. |

Details: [GITHUB_SETUP.md](docs/GITHUB_SETUP.md).

Secret handling:

- Keep secrets in `.env` (git-ignored) or a secrets manager; `config.yaml` names env variables (`*_env`) and never holds secret values.
- Each owner's secrets are `OWNER_<ID>_*` variables ([ENVIRONMENT.md](docs/ENVIRONMENT.md#per-owner-variables-owner_id_)). A named owner's GitHub, webhook, API, Jira and Slack credentials are its own; they never fall back to the global ones or another owner's. The **model key is the exception**: without `codex.api_key_env` or `OWNER_<ID>_OPENAI_API_KEY`, the owner's runs use the shared `OPENAI_API_KEY`, so its code is processed and billed under the shared model account. Give each owner its own model key when that matters.
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
- [ ] A dedicated model key per deployment or owner, with a spending limit; Jira/Slack service accounts with minimal access.
- [ ] `public_repos` settings reviewed before enabling the bot on a public repository.
- [ ] Logs go to a store with access control and retention.
- [ ] Postgres backups are encrypted and access-controlled (findings can quote code).
- [ ] The service runs from a current `main`.

Deployment: [DEPLOY.md](docs/DEPLOY.md).
