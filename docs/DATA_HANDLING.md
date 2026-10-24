# Data handling

What the service sends to the model provider, what it stores, and how to delete it. Public repository rules: [PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md). Security model: [SECURITY.md](../SECURITY.md).

## Sent to the model provider

Model calls go through the Codex CLI with the API key of the run's owner (`OPENAI_API_KEY` or `OWNER_<ID>_OPENAI_API_KEY`).

**Code review** (`prompts/review.md`): the prompt contains the repository name, PR number, branch, base/head SHA, PR title and body, the changed files' diffs (lockfiles, `.env*` files and binary/generated suffixes are skipped; each file is clipped at `codex.max_file_bytes`), findings from earlier runs on this PR, and the Jira issue text when one is linked and readable. Codex runs **in the cloned checkout** with a read-only sandbox, so the agent can read any file of the PR head and include it in its model context.

**PR title/description** (when `pr_description.enabled`): PR title and body, branch name, up to `pr_description.max_commit_messages` commit messages, a diff summary, and the Jira issue text.

Not sent: GitHub, Jira, Slack and database credentials are not passed to the review agent (the sandbox gets a minimal environment, see [ENVIRONMENT.md](ENVIRONMENT.md)), and neither are Slack data or other owners' data. The model key is present in the agent's environment, because the agent needs it to call the model; see [SECURITY.md](../SECURITY.md). The digest does not call the model.

For a public repository the review and PR text models do not receive Jira issue text; a separate read-only alignment check does, and publishes only a fixed result ([PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md#jira-jira_disclosure)). Retention on the provider side is governed by your agreement with it.

## Stored in Postgres

| Table | Content |
| --- | --- |
| `repositories` | Full name, owner, comment author identities. |
| `pull_requests` | Number, URL, title, bot title and its source hash, author, assignee, state, SHAs, head branch, issue key, draft/fork flags. |
| `review_runs` | Trigger, SHAs, status, model/prompt versions, duration, error/skip codes, ARQ job id, summary JSON, comment URL, `has_sensitive_content`, progress status bookkeeping. |
| `findings` | Severity, category, path, line, title, scenario, evidence, recommendation, visibility, confidence, status. Evidence can quote code. |
| `finding_transitions` | Status history with evidence. |
| `task_snapshots` | Jira key, Jira status, warning, whether a comment or transition was done. Not the issue text. |
| `digest_runs` | Digest bookkeeping. |

No GitHub tokens or API keys are stored in the database.

## Redis

The ARQ queue: job ids and arguments (run ids, repository, PR number), and short-lived results. Not the source of truth.

## Temporary files

The worker clones the PR head into a temporary directory (`ai-review-*` in the system temp directory, `TMPDIR`) and deletes it when the run ends. Codex runs with `--ephemeral`.

## Logs

The service logs to stdout: run ids, repositories, PR numbers, statuses, error codes and short error texts. Tokens are not logged on purpose. Your log pipeline decides retention.

## Retention and deletion

There is no automatic retention: rows stay until you delete them. Sticky comments, check runs and PR titles written to GitHub stay on GitHub.

To delete data of one repository, delete its rows in dependency order (`finding_transitions`, `findings`, `task_snapshots`, `review_runs`, `pull_requests`, `repositories`) with a backup taken first. `digest_runs` has no link to repositories: each row's JSON payload lists PRs of several repositories (repository, number, URL, assignee, head SHA), so delete the digest rows of that owner and period, or rewrite their payloads. Copies outside the database (GitHub comments and statuses, Jira comments, Slack messages, logs, backups) must be deleted separately. Removing an owner from the configuration does not delete its rows. To delete everything, drop the `postgres_data` and Redis volumes (`docker compose down -v`).
