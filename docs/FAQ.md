# FAQ

**Does the bot block merges?**
No. Reviews are advisory. The progress check run never reports `failure` for findings, only for bot errors ([PROGRESS_STATUS.md](PROGRESS_STATUS.md)).

**Does it run the PR's code?**
No. It clones the PR head and runs Codex in a read-only sandbox without network access; nothing is built, installed or executed ([SECURITY.md](../SECURITY.md#threat-model-and-trust-boundaries)).

**What is sent to the model provider?**
The PR's metadata, diffs, earlier findings, optional Jira text, and whatever files of the checkout the agent reads. Details: [DATA_HANDLING.md](DATA_HANDLING.md).

**Do I need a webhook?**
No. The simplest setup is the GitHub Actions trigger, which calls `POST /api/v1/reviews` with an API key ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#how-reviews-start)). A webhook needs `GITHUB_WEBHOOK_SECRET`.

**GitHub App or PAT?**
An App is preferred: its permissions are per repository and comments come from the App, not a person. A PAT is fine for a first test ([GITHUB_SETUP.md](GITHUB_SETUP.md)).

**`REVIEW_API_KEY` vs `GITHUB_TOKEN`?**
`REVIEW_API_KEY` is a password you generate to protect this service's API. `GITHUB_TOKEN` (or `GITHUB_PAT`) is a GitHub token the service uses to read and comment.

**The API answers `202` but nothing appears on the PR.**
See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#api-answers-202-nothing-appears-on-github). Usual causes: the worker is not running, no model quota (`CODEX_QUOTA`), a skip (draft, fork, author permission, size guard) or missing GitHub permissions.

**Why was the same commit not reviewed again?**
Webhooks and `force: false` reuse a completed run for the same SHA. A manual request (default `force: true`) starts a new run.

**Can one deployment serve several organizations?**
Yes, with an `owners:` block ([MULTI_OWNER.md](MULTI_OWNER.md)).

**Can I review public repositories?**
Yes. Security findings are redacted and Jira disclosure is limited by default ([PUBLIC_DISCLOSURE.md](PUBLIC_DISCLOSURE.md)).

**Can I change the review instructions?**
Yes: `codex.prompt_file` (and `pr_description.prompt_file`), see [DEPLOY.md](DEPLOY.md#configyaml).

**Which languages are supported?**
`en` and `ru` for review output (`language.summary`, `language.details`); PR descriptions accept any language code ([CONFIGURATION.md](CONFIGURATION.md#language)).

**Does it create Jira issues?**
No. It only comments on a linked issue and, when enabled, moves it to `rework_status` on P0/P1 findings.

**Large PRs?**
Above `size_guard.hard` or `codex.max_files` / `max_diff_lines` the run is skipped with `PR_TOO_LARGE_FOR_AI_REVIEW`. The `autoreview:force` label bypasses the size guard ([PR_DESCRIPTION.md](PR_DESCRIPTION.md#size-guard)).

**How do I delete stored data?**
See [DATA_HANDLING.md](DATA_HANDLING.md#retention-and-deletion).

**How do I report a vulnerability?**
Privately, through GitHub Security Advisories ([SECURITY.md](../SECURITY.md#reporting-a-vulnerability)).
