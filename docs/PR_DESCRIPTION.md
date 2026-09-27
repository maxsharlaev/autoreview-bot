# Optional PR description drafts

The feature is disabled by default. It runs **after the review is published and committed** and drafts a PR description from the current title, branch, PR commits, changed-file summary, existing body and an available Jira issue. A changed head SHA cancels publication of the draft. Timeout or failure here cannot erase or repeat the completed review. The generator uses Codex in a read-only sandbox without GitHub write credentials; only the orchestrator posts the result. Draft and fork PRs are skipped because the review flow excludes them.

```yaml
pr_description:
  enabled: true
  mode: comment # comment | fill_empty | append
  title_mode: "off" # off | until_human_edit | when_invalid_or_inconsistent
  check_title_relevance: true
  timeout_seconds: 180
  max_commit_chars: 12000
  language: auto # auto or any BCP 47 language code
  prompt_file: "" # optional: prompts/local/pr_description.md

size_guard:
  soft: {commits: 50, changed_lines: 5000}
  hard: {commits: 150, changed_lines: 20000}
  override_label: autoreview:force
```

| Mode | Behavior |
| --- | --- |
| `comment` | Posts a separate sticky suggestion marked `<!-- autoreview-bot:pr-description -->`. The author can copy it. |
| `fill_empty` | Fills an empty body or one exactly matching a default repository PR template. Later updates replace only an unchanged generated block. |
| `append` | Adds a clearly marked generated block below author text and updates only that block. |

Generated body blocks and suggestion comments store a checksum of bot text. If someone edits them, the service leaves them alone. In `fill_empty`, adding author text outside the block also stops automatic updates. The worker re-reads the PR body and head before writing; GitHub's body update API does not provide an atomic compare-and-swap, so a concurrent edit in the final request window still needs operational caution.

`title_mode` controls title changes independently of the description mode. `off` never changes the title. Both write modes change only a blank title, a title identical to the branch name, or a title still equal to the last title written by this bot. `until_human_edit` updates those titles when the source changes; `when_invalid_or_inconsistent` also requires a placeholder or clear mismatch with commit subjects and bodies. A human title such as `Dev` is preserved and its mismatch is reported in **Title check**. `always` is a deprecated alias for `until_human_edit` and logs a warning. Bot ownership and source hash are stored in the database; migration `002_bot_title` is required. A suggestion must follow `type(scope)?: subject`, retain any Jira key from the previous title, and contain no control characters or URLs. The bot re-reads the title and SHA before updating. A concurrent edit during the final API request can still race with the update.

Template detection checks `pull_request_template.md` in `.github/`, the repository root and `docs/` on the PR base branch (including uppercase filenames), plus Markdown files in `.github/PULL_REQUEST_TEMPLATE/`. Only an exact match after line-ending and outer-whitespace normalization is replaced.

The default prompt is [`prompts/pr_description.md`](../prompts/pr_description.md). To override it, copy it to `prompts/local/pr_description.md` and set `pr_description.prompt_file` to that path. The directory is ignored by Git and Docker build context but mounted read-only into the worker. Changes to the file are read on the next review; changing `config.yaml` needs a worker restart. Keep the JSON-only and untrusted-input rules in custom prompts. Custom prompts must produce the current schema, including `output_language` and title assessment fields.

Output is checked against [`schemas/pr_description_output.json`](../schemas/pr_description_output.json). Sections are rendered as plain text with HTML and mentions escaped; bare URLs are broken up to prevent automatic linking. Empty sections and the linked-task section without Jira are omitted. `pr_description.language: auto` detects English or Russian from human title, then human body, then commits; sparse input falls back to `language.details`. Explicit language codes are passed to the model. The former `pr_text.language` key remains a fallback for existing installations. Section headings come from fixed English/Russian dictionaries, with English headings for other language codes. English and Russian output is checked by script plus the model's declared language; other language codes use a separate read-only Codex language check. A mismatch causes one regeneration, then publication is skipped. Conventional-commit type and scope remain English. The bot does not claim tests passed without evidence.

The size guard uses PR commit and line totals from the webhook before any model call. If a webhook omits these fields, or for a manual run, it uses the PR metadata already loaded for the review, without another request. Above a soft threshold, review runs but description and title generation are skipped. Above a hard threshold, the run is skipped with one sticky notice; later pushes do not repeat that notice. Adding the `autoreview:force` label triggers a new run and bypasses both thresholds. At an exact threshold, the PR is still allowed.

The commit prompt contains only the first line of each useful commit message, grouped by conventional type. Fixup, squash, merge and WIP messages are omitted. `max_commit_chars` bounds the selected subjects; the prompt states the total and how many commits were not shown. The adapter reads no more than GitHub's 250 PR commits.
