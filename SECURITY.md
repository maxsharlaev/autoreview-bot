# Security Policy

## Supported versions

Only the `main` branch receives security updates. There are no versioned releases at this time.

| Branch | Supported |
| ------ | --------- |
| main   | Yes       |

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Please report security issues through [GitHub Security Advisories](https://github.com/maxsharlaev/autoreview-bot/security/advisories/new). This allows the maintainer to discuss and address the issue privately before public disclosure.

When reporting, please include:

- A description of the vulnerability
- Steps to reproduce (if applicable)
- Potential impact
- Any suggested fixes (optional)

You can expect an initial response within 7 days. The maintainer will work with you to understand and address the issue.

## Security considerations

This service handles sensitive credentials:

- **GitHub tokens** (App private key or PAT) — for repository access, cloning, and PR comments
- **OpenAI API key** — passed to Codex CLI for reviews
- **Jira API token** — for issue access (if enabled)
- **Slack bot token** — for digest posting (if enabled)
- **`REVIEW_API_KEY`** — protects the review API endpoints

### Best practices for deployment

1. **Never commit secrets** — use `.env` files (excluded from Git) or a secrets manager.
2. **Restrict network access** — keep database and Redis ports private; do not expose `/metrics` to the public internet.
3. **Use HTTPS** — terminate TLS at a reverse proxy before the API.
4. **Rotate credentials** — especially after any suspected exposure.
5. **Limit repository access** — configure `github.allowed_repos` to restrict which repositories the service will review.

See [docs/DEPLOY.md](docs/DEPLOY.md) for deployment guidance and [docs/GITHUB_SETUP.md](docs/GITHUB_SETUP.md) for credential setup.
