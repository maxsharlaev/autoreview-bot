# Public repository disclosure

The worker confirms repository visibility through the GitHub API on every run. Webhook visibility is kept as an extra, conservative hint. Missing or conflicting visibility is treated as **public**. Private repositories keep the full review output.

```yaml
public_repos:
  jira_disclosure: key_only # none | key_only | full
  security_findings: redact # redact | redact_all | full
```

Both keys can be overridden per owner ([CONFIGURATION.md](CONFIGURATION.md#public_repos)).

## Jira (`jira_disclosure`)

For a public repository:

- `key_only` (default) shows the Jira key as plain text, without a link;
- `none` removes the linked key from bot output;
- `full` explicitly allows the linked task's content (summary, acceptance criteria) in the generated PR description.

The review model and the PR description/title generator do not receive issue text fetched from Jira for public repositories. A separate read-only alignment check receives it and publishes only a fixed result: `matches`, `partial`, `mismatch` or `unknown`. When Jira data is available this adds one model call, latency and model cost to each public PR review.

## Security findings (`security_findings`)

With `redact` (default) a finding is redacted when its category is `security`, or when its title, scenario, evidence or recommendation matches security keywords (injection, XSS, SSRF, CSRF, RCE, path traversal, deserialization, auth bypass, privilege escalation, secret, token leak, credential and similar) or a CWE reference (`CWE-\d+`). The keyword heuristic cannot guarantee that every mislabeled finding is caught; for strict setups use `redact_all`, which redacts every finding regardless of category.

A redacted finding shows only severity, path/line and a generic label. Full details stay in the database and go to the configured Jira comment or Slack channel. Use a private Slack channel restricted to the intended reviewers. If neither is configured, the PR comment says that details are hidden.

`full` explicitly restores detailed findings in GitHub, except details recorded while the repository was private.

## Previous findings

Previous findings remain available for status tracking. Details of findings created while the repository was private (`details_visibility`) are withheld from the model context of a public run; details created while public are reused.

## Visibility changes

If the private-visibility recheck before publication fails, or reports a public repository, the run publishes the generic public-safe version, and the same reduced text goes to Jira and Slack for that run. If the PR head changed, the run fails with `HEAD_CHANGED`. Comments published earlier are not rewritten.

See also [DATA_HANDLING.md](DATA_HANDLING.md) and the `has_sensitive_content` flag in [PROGRESS_STATUS.md](PROGRESS_STATUS.md#has_sensitive_content).
