<!-- section: documentation -->
# Documentation

English documents are primary; [docs/ru](ru/README.md) is a Russian mirror. Every heading in both is preceded by a `<!-- section: <id> -->` marker with an id taken from the English heading; the mirror uses the same ids in the same order, and `tests/test_docs.py` checks this. Tag new sections the same way in both languages.

<!-- section: getting-started -->
## Getting started

- [Deploy](DEPLOY.md): Compose stack, network, TLS, acceptance criteria.
- [GitHub setup](GITHUB_SETUP.md): GitHub App or PAT, webhook, Actions trigger.
- [FAQ](FAQ.md).

<!-- section: reference -->
## Reference

- [Configuration](CONFIGURATION.md): every `config.yaml` key, type, default and per-owner override.
- [Environment variables](ENVIRONMENT.md): every variable and the service that reads it.
- [HTTP API](API.md): endpoints, authentication, request fields, responses and error codes.
- [Multiple owners](MULTI_OWNER.md): several GitHub organizations in one deployment.
- [Architecture](ARCHITECTURE.md): pipeline, components, data model.
- [PR description drafts](PR_DESCRIPTION.md) and size guard.
- [Progress status](PROGRESS_STATUS.md): check run / commit status, and the [review comment](PROGRESS_STATUS.md#review-comment) format (review time, severity lights).
- [Public repository disclosure](PUBLIC_DISCLOSURE.md).

<!-- section: operations -->
## Operations

- [Troubleshooting](TROUBLESHOOTING.md): triggers, health checks, symptoms, error and skip codes.
- [Observability](OBSERVABILITY.md): metrics, dashboards, alerts.
- [Upgrade and rollback](UPGRADE.md): migrations and downgrades.

<!-- section: security-and-data -->
## Security and data

- [Security policy](../SECURITY.md): reporting, threat model, permissions, hardening.
- [Data handling](DATA_HANDLING.md): what is sent to the model, stored and retained.
- [Public release checklist](PUBLIC_RELEASE_CHECKLIST.md).

<!-- section: project -->
## Project

- [Roadmap](ROADMAP.md)
- [Contributing](../CONTRIBUTING.md)
- [Changelog](../CHANGELOG.md)
