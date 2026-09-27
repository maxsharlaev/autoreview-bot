# Optional PR description drafts

The feature is disabled by default. It runs after a successful code review and drafts a PR description from the current title, branch, PR commits, changed-file summary, existing body and an available Jira issue. A changed head SHA cancels publication of the draft. The generator uses Codex in a read-only sandbox without GitHub write credentials; only the orchestrator posts the result.

```yaml
pr_description:
  enabled: true
  mode: comment # comment | fill_empty | append
  title_mode: "off" # off | always | when_invalid_or_inconsistent
  check_title_relevance: true
  prompt_file: "" # optional: prompts/local/pr_description.md
```

| Mode | Behavior |
| --- | --- |
| `comment` | Posts a separate sticky suggestion marked `<!-- autoreview-bot:pr-description -->`. The author can copy it. |
| `fill_empty` | Fills an empty body or one exactly matching a default repository PR template. Later updates replace only an unchanged generated block. |
| `append` | Adds a clearly marked generated block below author text and updates only that block. |

Generated body blocks and suggestion comments store a checksum of bot text. If someone edits them, the service leaves them alone. In `fill_empty`, adding author text outside the block also stops automatic updates. The worker re-reads the PR body and head before writing; GitHub's body update API does not provide an atomic compare-and-swap, so a concurrent edit in the final request window still needs operational caution.

`title_mode` controls title changes independently of the description mode. `off` never changes the title; `always` applies a valid, evidence-based suggestion; `when_invalid_or_inconsistent` changes a placeholder (for example `Dev`), a title identical to the branch name, or a title clearly inconsistent with commit subjects and bodies. An `uncertain` assessment never triggers the inconsistency rule. `check_title_relevance` controls that comparison; when false, the conditional mode still catches placeholders and branch-name titles. With the check enabled, an unresolved mismatch is shown in a **Title check** section of the draft. The bot skips empty or unsafe suggestions and re-reads the title and SHA before updating. A concurrent edit during the final API request can still race with the update.

Template detection checks `pull_request_template.md` in `.github/`, the repository root and `docs/` on the PR base branch (including uppercase filenames), plus Markdown files in `.github/PULL_REQUEST_TEMPLATE/`. Only an exact match after line-ending and outer-whitespace normalization is replaced.

The default prompt is [`prompts/pr_description.md`](../prompts/pr_description.md). To override it, copy it to `prompts/local/pr_description.md` and set `pr_description.prompt_file` to that path. The directory is ignored by Git and Docker build context but mounted read-only into the worker. Changes to the file are read on the next review; changing `config.yaml` needs a worker restart. Keep the JSON-only and untrusted-input rules in custom prompts.

Output is checked against [`schemas/pr_description_output.json`](../schemas/pr_description_output.json). Sections are rendered as plain text with HTML and mentions escaped. Headings follow `language.details` (`en` or `ru`). The linked-task section is omitted when Jira context is unavailable. The bot does not claim tests passed without evidence.
