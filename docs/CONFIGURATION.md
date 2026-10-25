# Configuration reference (`config.yaml`)

`config.yaml` holds non-secret settings. Secrets live in the environment (see [ENVIRONMENT.md](ENVIRONMENT.md)); YAML only names the variables that hold them. Start from [`config.example.yaml`](../config.example.yaml).

- The file path is `CONFIG_PATH` (default `config.yaml`, relative to the working directory; `/app/config.yaml` in the Compose images).
- A missing file means all defaults below.
- Unknown top-level keys are ignored, so an older image can read a newer file. Inside `owners.<id>` unknown keys are rejected.
- The API and the worker read the file once at startup. **Restart both after any change** (`docker compose up -d --force-recreate api worker`). A prompt file named in the config is re-read on every review.
- An invalid file (wrong type, value out of range, bad owner block) stops the API and the worker at startup with an error naming the field. Secret values are never printed.

Column **Owner** says whether the key can be overridden in an `owners.<id>` block (see [MULTI_OWNER.md](MULTI_OWNER.md)). Overrides are partial and deep-merged over the top-level value; the merged section is validated like the global one.

## `github`

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `github.allowed_repos` | list of `owner/repo` | `[]` | own list in `owners.<id>.github.allowed_repos` | Repositories the legacy (`default`) owner may review. Exact, case-insensitive names. **An empty list allows every repository** and logs a startup warning; set an explicit list in production. Repositories outside a non-empty list are skipped with `repo_not_allowed`. |
| `github.app_id` | int | `0` | — | Not used for credentials. The legacy owner's App id comes from `GITHUB_APP_ID`. Kept for compatibility. |
| `github.installation_id` | int | `0` | — | Not used for credentials. The legacy owner's installation comes from `GITHUB_INSTALLATION_ID` (or is looked up). Kept for compatibility. |

Owner GitHub blocks (`owners.<id>.github.*`) are listed in [Owners](#owners).

## `jira`

Used only by the legacy owner. Named owners use only their own `owners.<id>.jira` block; nothing is inherited.

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `jira.base_url` | string | `""` | own block | Jira site URL. `JIRA_BASE_URL` overrides it when set. |
| `jira.email` | string | `""` | own block | Service account email. `JIRA_EMAIL` overrides it when set. |
| `jira.projects` | map `KEY: {rework_status}` | `{}` | own block | Allowed project keys. An empty map allows any key found in the PR. |
| `jira.projects.<KEY>.rework_status` | string | `"In Progress"` | own block | Status the issue moves to when P0/P1 findings exist and `features.jira_transition` is on. |

Jira is active only when base URL, email and `JIRA_API_TOKEN` are all set. The service account needs browse, comment and transition permissions on those projects. The service never creates issues.

## `slack`

Used only by the legacy owner; named owners use their own `owners.<id>.slack` block.

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `slack.enabled` | bool | `false` | own block (default `true` there) | Post notifications and the digest. Needs `SLACK_BOT_TOKEN` and a channel. With `false` the digest is stored in the database only. |
| `slack.channel` | string | `""` | own block | Channel name or id. Use a private channel: unredacted finding details can be sent there (see [PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md)). |

## `schedule`

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `schedule.review_digest.enabled` | bool | `true` | — | Register the open-PR digest job in the worker. |
| `schedule.review_digest.cron` | string | `"0 * * * *"` | — | When the digest runs. **Only the minute and hour fields are used, and only plain numbers:** `"15 * * * *"` runs hourly at :15, `"0 9 * * *"` daily at 09:00. Any other form of a field (`*/15`, lists, ranges) falls back to minute `0` / every hour; day, month and weekday are ignored. Worker time zone. |

The digest runs per owner and only for owners with `features.digest_enabled: true`.

## `features`

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `features.jira_comment` | bool | `true` | yes | Comment on the linked Jira issue after a review. |
| `features.jira_transition` | bool | `true` | yes | Move the issue to `rework_status` when P0/P1 findings exist. |
| `features.digest_enabled` | bool | `true` | yes | Include this owner in the scheduled digest. |

## `language`

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `language.summary` | `en` \| `ru` | `en` | yes | Language of the review summary and finding titles. |
| `language.details` | `en` \| `ru` | `en` | yes | Language of finding scenario, evidence, recommendation and PR comment labels. Fallback for the PR description language. |

Jira and Slack text currently use English labels.

## `pr_description`

Optional PR description and title drafts, off by default. Behaviour: [PR_DESCRIPTION.md](PR_DESCRIPTION.md).

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `pr_description.enabled` | bool | `false` | yes | Draft a description after the review is published. |
| `pr_description.mode` | `comment` \| `fill_empty` \| `append` | `comment` | yes | Separate sticky suggestion; fill an empty body or unchanged template; or a marked block below the author's text. |
| `pr_description.title_mode` | `off` \| `until_human_edit` \| `when_invalid_or_inconsistent` | `off` | yes | Title updates. `always` is a deprecated alias for `until_human_edit` (logs a warning); `false` means `off`. Quote `"off"` in YAML. |
| `pr_description.check_title_relevance` | bool | `true` | yes | Compare the title with commit messages and report mismatches in the draft. |
| `pr_description.timeout_seconds` | int, 1–600 | `180` | yes | Total budget for this optional step. |
| `pr_description.max_commit_chars` | int, 1–50000 | `12000` | yes | Character budget for commit subjects in the prompt. |
| `pr_description.max_commit_messages` | int, 1–250 | `250` | yes | Legacy cap on the number of commit messages read. GitHub returns at most 250 PR commits. |
| `pr_description.language` | `auto` or BCP 47 code | unset | yes | Draft language. Unset falls back to `pr_text.language`, then `language.details`. `auto` detects English/Russian from the human title, body, then commits. |
| `pr_description.prompt_file` | path | `""` | yes | Replacement prompt, e.g. `prompts/local/pr_description.md`. Empty uses [`prompts/pr_description.md`](../prompts/pr_description.md). A missing file fails the step. |

## `pr_text`

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `pr_text.language` | `auto` or BCP 47 code | unset | — | Legacy name of `pr_description.language`, used only when that key is unset. |

## `size_guard`

Checked before any model call. Details: [PR_DESCRIPTION.md](PR_DESCRIPTION.md#size-guard).

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `size_guard.soft.commits` | int > 0 | `50` | yes | Above this, the review runs but description/title drafts are skipped. |
| `size_guard.soft.changed_lines` | int > 0 | `5000` | yes | Same, for added + deleted lines. |
| `size_guard.hard.commits` | int > 0, ≥ soft | `150` | yes | Above this, the run is skipped with one notice comment (`PR_TOO_LARGE_FOR_AI_REVIEW`). |
| `size_guard.hard.changed_lines` | int > 0, ≥ soft | `20000` | yes | Same, for changed lines. |
| `size_guard.override_label` | non-empty string | `autoreview:force` | yes | Adding this label starts a new run that bypasses both thresholds. Compared case-insensitively. |

A value exactly at a threshold is allowed.

## `public_repos`

Applies only to repositories GitHub reports as public (unknown visibility counts as public). Details: [PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md).

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `public_repos.jira_disclosure` | `none` \| `key_only` \| `full` | `key_only` | yes | What bot output may show about the linked Jira issue. |
| `public_repos.security_findings` | `redact` \| `redact_all` \| `full` | `redact` | yes | Redact security findings (by category and keywords), all findings, or none. |

## `progress_status`

Check run or commit status on the PR head. Details: [PROGRESS_STATUS.md](PROGRESS_STATUS.md).

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `progress_status.enabled` | bool | `true` | yes | Report progress. |
| `progress_status.mode` | `auto` \| `check_run` \| `commit_status` \| `off` | `auto` | yes | `auto`: check run for a GitHub App, commit status for a PAT. |
| `progress_status.reaction` | bool | `false` | yes | Add an `eyes` reaction to the PR while a worker reviews it. |
| `progress_status.timeout_seconds` | float, > 0 and ≤ 30 | `5` | yes | Cap per GitHub call. |

## `codex`

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `codex.model` | string | `gpt-5.6-sol` | yes | Model passed to Codex CLI. |
| `codex.reasoning_effort` | string | `medium` | yes | Codex reasoning effort. |
| `codex.api_key_env` | env var name | — | **owner only** | Model key variable for this owner (required when set, in both the API and the worker). Without it: `OWNER_<ID>_OPENAI_API_KEY`, else the shared `OPENAI_API_KEY`. |
| `codex.prompt_file` | path | `""` | — | Replacement review prompt, e.g. `prompts/local/review.md`. Empty uses [`prompts/review.md`](../prompts/review.md). Relative to the working directory (`/app` in Docker). A missing file fails the review. `{{summary_language}}` and `{{details_language}}` are substituted. |
| `codex.sandbox` | string | `read-only` | — | Codex `--sandbox` value. Keep `read-only`. |
| `codex.approval_policy` | string | `never` | — | Codex approval policy. Keep `never`: nobody answers prompts in a worker. |
| `codex.timeout_seconds` | int | `600` | — | Timeout of one Codex call. Keep it below `WORKER_JOB_TIMEOUT`. |
| `codex.max_files` | int | `100` | — | More changed files in the PR than this, counting skipped files too (lockfiles, binaries and other files not sent to the model): no model call, the PR gets the `PR_TOO_LARGE_FOR_AI_REVIEW` comment. |
| `codex.max_diff_lines` | int | `20000` | — | More diff lines than this: same as `max_files`. |
| `codex.max_findings` | int | `20` | — | Maximum findings kept from one review. |
| `codex.max_file_bytes` | int | `524288` | — | Per-file patch bytes sent to the model; longer patches are truncated. |

Common lockfiles, `.env*` files and files with image, PDF, archive, font or compiled-binary suffixes are left out of the model context. Other generated, minified or vendored files (for example `.min.js` or `.map`) are not detected and are sent like source. Full list: [DATA_HANDLING.md](DATA_HANDLING.md#sent-to-the-model-provider).

## `routing`

| Key | Type | Default | Owner | Description |
| --- | --- | --- | --- | --- |
| `routing.unclaimed` | `default` \| `reject` | `default` | — | Where a repository that no owner claims goes: the default owner, or rejected as `unknown_owner`. Unknown keys under `routing` are rejected. |

## `owners`

Optional. Without it the service runs as one owner, `default`, built from the environment and the sections above. Modes, routing and examples: [MULTI_OWNER.md](MULTI_OWNER.md).

Owner ids match `^[a-z0-9][a-z0-9_-]{0,63}$`. Env var names in `*_env` keys match `^[A-Z][A-Z0-9_]*$`; a literal secret is rejected. `<ID>` below is the owner id upper-cased with `-` replaced by `_`.

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `owners.<id>.default` | bool | `false` | Routing fallback owner (modes C/D, or instead of `default` in mode B). At most one. |
| `owners.<id>.enabled` | bool | `true` | `false` stops new work for the owner; queued runs are skipped as `owner_disabled` and its open statuses are closed as skipped. |
| `owners.<id>.aliases` | list of ids | `[]` | Other ids this owner answers to. `aliases: [default]` inherits rows recorded before multi-owner support (only one owner may, and not in mode B). |
| `owners.<id>.github.auth` | `app` \| `pat` | `app` | Credential type. |
| `owners.<id>.github.app_id` | int | — | App id (App only). |
| `owners.<id>.github.app_id_env` | env var name | `OWNER_<ID>_GITHUB_APP_ID` | Variable holding the App id (App only). |
| `owners.<id>.github.private_key_env` | env var name | `OWNER_<ID>_GITHUB_APP_PRIVATE_KEY` | Variable holding the PEM or a path to it (App only). |
| `owners.<id>.github.private_key_file` | path | — | PEM file instead of a variable (App only). Must exist at startup. |
| `owners.<id>.github.installation_id` | int | `0` | App installation; `0` takes it from the webhook or looks it up. A non-zero id also routes webhooks of that installation to this owner (App only). |
| `owners.<id>.github.token_env` | env var name | `OWNER_<ID>_GITHUB_TOKEN` | Variable holding the PAT (PAT only). |
| `owners.<id>.github.webhook_secret_env` | env var name | `OWNER_<ID>_GITHUB_WEBHOOK_SECRET` | Webhook secret. Missing or shorter than 16 characters disables webhooks for this owner only. |
| `owners.<id>.github.allowed_repos` | list | `[]` | Claims: exact `org/repo` or `org/*`. Empty accepts any repository routed to the owner (startup warning). Two owners may not claim the same entry. |
| `owners.<id>.api.key_env` | env var name | `OWNER_<ID>_REVIEW_API_KEY` | API key scoped to this owner (optional). Must differ from other owners' keys and from `REVIEW_API_KEY`. |
| `owners.<id>.jira.base_url` | string | `""` | Jira site. Both `base_url` and `email` are needed, or Jira is off for the owner (warning). |
| `owners.<id>.jira.email` | string | `""` | Jira account. |
| `owners.<id>.jira.api_token_env` | env var name | `OWNER_<ID>_JIRA_API_TOKEN` | Jira token. |
| `owners.<id>.jira.projects` | map | `{}` | As `jira.projects`. |
| `owners.<id>.slack.enabled` | bool | `true` | Slack for this owner when the block exists. |
| `owners.<id>.slack.channel` | string | `""` | Channel; required when enabled. |
| `owners.<id>.slack.bot_token_env` | env var name | `OWNER_<ID>_SLACK_BOT_TOKEN` | Slack bot token. |
| `owners.<id>.public_repos`, `.language`, `.features`, `.pr_description`, `.size_guard`, `.progress_status` | partial sections | — | Overrides of the top-level sections; every field marked **yes** above. |
| `owners.<id>.codex` | `model`, `reasoning_effort`, `api_key_env` | — | Model, effort and model key for this owner. Other `codex` fields stay global. |

Owners without a `jira` or `slack` block make no Jira requests and post nothing to Slack. The same App installation on two owners is an error; the same PAT on two owners is a warning.
