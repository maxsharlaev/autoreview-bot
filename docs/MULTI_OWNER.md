# Multiple owners

One deployment can serve several GitHub owners (organizations or accounts), each with its own GitHub credentials, webhook secret, API key, Jira, Slack, policies and model key. A config without `owners:` keeps working exactly as before. How it works inside: [ARCHITECTURE.md](ARCHITECTURE.md#multiple-owners).

## Modes

"Legacy GitHub credentials" means `GITHUB_TOKEN`/`GITHUB_PAT`, or both `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY`, set in the environment.

| Mode | Detected by | Owners | Default owner |
| --- | --- | --- | --- |
| A. Legacy | no `owners:` block | `default`, built from today's env and top-level `github`/`jira`/`slack` | `default` |
| B. Legacy + extra owners | legacy GitHub credentials in env **and** an `owners:` block | `default` plus the listed owners | `default`, unless an owner has `default: true` |
| C. Single owner block | no legacy GitHub credentials, one owner | that owner | that owner |
| D. Several owners | no legacy GitHub credentials, two or more owners | the listed owners | the one with `default: true`, else the first one (startup warning) |

Two `default: true` owners, an owner named `default` in mode B, the same repo or `org/*` claimed by two owners, the same App installation or API key on two owners, an owner API key equal to `REVIEW_API_KEY`, or a missing required secret stop startup with an error that names the variable, never its value.

## Routing

A request goes to: the explicit owner (`/api/v1/pull-request/<id>`, `X-Review-Owner` header, `?owner=` or an owner-scoped API key); else the owner listing the exact `org/repo` in `github.allowed_repos`; else the owner with `org/*`; else the owner whose non-zero `installation_id` matches the webhook; else `routing.unclaimed: default` (default owner) or `reject` (`unknown_owner`). The chosen owner's allowlist then applies. An explicit owner on a repo claimed by another owner gets 409 `owner_repo_conflict`; a webhook signed with one owner's secret for another owner's repo gets 403 `owner_signature_mismatch`. `REVIEW_API_KEY` is the operator key for all owners; `owners.<id>.api.key_env` is a key scoped to one owner, which sees only its own runs.

## Secrets

YAML holds only env var names (`*_env`) or key file paths; a literal secret is rejected. Without an explicit `*_env` the name is `OWNER_<ID>_<NAME>` (`<ID>` upper-cased, `-` → `_`): `GITHUB_TOKEN`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, `REVIEW_API_KEY`, `JIRA_API_TOKEN`, `SLACK_BOT_TOKEN`, `OPENAI_API_KEY`; for example `OWNER_EXAMPLE_ORG_GITHUB_TOKEN`. `DATABASE_URL`, `REDIS_URL`, `OPENAI_API_KEY` (shared default model key) and worker/log/metrics settings stay shared. An invalid webhook secret disables webhooks only for that owner.

Secret names in full: [ENVIRONMENT.md](ENVIRONMENT.md#per-owner-variables-owner_id_). Every owner key: [CONFIGURATION.md](CONFIGURATION.md#owners).

## Example: two organizations with PATs

```yaml
routing:
  unclaimed: reject          # repositories of any other account are refused
owners:
  org-a:
    default: true
    github:
      auth: pat              # token from OWNER_ORG_A_GITHUB_TOKEN
      allowed_repos: ["org-a/*"]
  org-b:
    github:
      auth: pat              # token from OWNER_ORG_B_GITHUB_TOKEN
      allowed_repos: ["org-b/*"]
```

A fine-grained PAT belongs to one resource owner (a user or one organization), so each owner needs its own token. Leave the legacy `GITHUB_TOKEN` empty to stay in mode D.

## Connecting another organization

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

## Keeping pre-multi-owner history

Findings, review runs and comment-author records written before the `owners:` block existed are stored under the legacy owner id `default`. They are bound explicitly, never through the `default: true` flag:

- legacy env credentials only, or legacy env plus an `owners:` block: the legacy `default` owner keeps them;
- `owners:` block only: the owner that lists `default` in `aliases` owns them (only one owner may);
- otherwise nobody sees them: previous findings, previous head SHA, digest blockers and trusted comment authors from that history are ignored, and the API/worker log a startup warning.

When moving from legacy env credentials to named owners only, add the alias to the owner that should inherit that history:

```yaml
owners:
  example-org:
    default: true          # routing fallback only; moving it does not move history
    aliases: [default]     # inherits rows recorded as owner 'default'
    github:
      auth: pat
```


## Jira and Slack per owner

A named owner uses only its own `jira` and `slack` blocks; nothing is inherited from the top-level sections or the legacy `default` owner. Without a `jira` block the owner makes no Jira requests; without a `slack` block it never posts, including the hourly digest. Jira reads, comments and transitions use the owner's site, account and `projects` (allowed project keys and their `rework_status`). Each owner with Slack gets its own digest in its own channel. Secrets are env var names: `api_token_env` / `bot_token_env`, or by default `OWNER_<ID>_JIRA_API_TOKEN` / `OWNER_<ID>_SLACK_BOT_TOKEN`. To share a Jira account or Slack bot between owners, point both at the same env var name.

```yaml
owners:
  example-org:
    github: { auth: pat, allowed_repos: [example-org/*] }
    jira:
      base_url: https://example-org.atlassian.net
      email: review-bot@example-org.example
      api_token_env: OWNER_EXAMPLE_ORG_JIRA_API_TOKEN
      projects:
        ABC: { rework_status: "In Progress" }
    slack:
      enabled: true
      channel: "#example-org-review"
      bot_token_env: OWNER_EXAMPLE_ORG_SLACK_BOT_TOKEN
```

A disabled owner's webhook URL `/api/v1/pull-request/<id>` answers `owner_disabled` only to requests signed with that owner's webhook secret; other requests get 401.


## Policies and model per owner

`public_repos`, `language`, `features` (`jira_comment`, `jira_transition`, `digest_enabled`), `pr_description`, `size_guard` and `progress_status` can be overridden per owner. Overrides are partial and deep-merged over the top-level sections: unset fields keep the global value, and the merged section is validated like the global one (for example, `size_guard.hard` must stay at least `size_guard.soft`). They apply to everything that reads them for that owner's runs: the review prompt and comment, public-repository redaction, the size guard and its `override_label` (also in the webhook label check), PR description drafts, Jira comments/transitions and the digest. The digest schedule stays global; `features.digest_enabled` decides per owner.

`codex.model` and `codex.reasoning_effort` can be set per owner; other `codex` fields (sandbox, approval policy, limits, prompt file) stay global. The model key for an owner's runs is `codex.api_key_env` (required when set), else `OWNER_<ID>_OPENAI_API_KEY` if present, else the shared `OPENAI_API_KEY`. Only that key reaches the Codex sandbox, and only for that owner's runs; other owners' keys and owner secrets never enter the Codex or git environment.

```yaml
owners:
  example-org:
    github: { auth: pat, allowed_repos: [example-org/*] }
    public_repos: { security_findings: redact_all }
    language: { summary: ru, details: ru }
    features: { jira_transition: false, digest_enabled: false }
    size_guard:
      hard: { commits: 100 }
      override_label: example:force-review
    codex:
      model: gpt-5.6-sol
      reasoning_effort: high
      api_key_env: OWNER_EXAMPLE_ORG_MODEL_KEY
```


## Upgrade and rollback

1. Deploy the new image and run `alembic upgrade head` (Compose does this in `migrate`) with the config untouched: behaviour is unchanged and logs/metrics show `owner=default`.
2. Add `owners.<id>` and its `OWNER_<ID>_*` env, then restart API and worker; the API startup log shows the number of owners, the default owner and any owner config warnings.
3. Connect the new owner's events: install the same GitHub App in the other organization (same env names, `allowed_repos: [org-b/*]` or `installation_id`), or give the owner its own App/PAT with a webhook to `/api/v1/pull-request/<id>`.
4. Check a manual `POST /api/v1/reviews` with and without `X-Review-Owner`, a test PR in each organization, and `owner` in `GET /api/v1/reviews/{id}`.
5. Optional: move the legacy setup into a named owner with `default: true`, `aliases: [default]` and `*_env` pointing at the existing variable names (see above).
6. Rollback within multi-owner versions: remove or disable the `owners.<id>` block and restart; runs queued for that owner are skipped with `owner_not_configured` / `owner_disabled`, and its rows stay in the database. A disabled owner's open progress statuses are closed as `skipped`; a removed owner's can't be closed (see [PROGRESS_STATUS.md](PROGRESS_STATUS.md#owners-disabled-or-removed)).
7. Rollback to an image from before multi-owner support: with the new image, run `alembic downgrade 004_finding_details_visibility` first, then deploy the old image. The old image cannot use the upgraded schema as is: after `007_findings_owner_id`, `findings.owner_id` is required without a default, and after `006_comment_authors_objects` comment authors are stored as objects. If findings of several owners share a stable id on one pull request, the `007` downgrade refuses and reports the count; set `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1` to keep the repository owner's row (or the oldest) and delete the others together with their transitions.
