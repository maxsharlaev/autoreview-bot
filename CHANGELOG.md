# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Multiple GitHub owners** ([#9](https://github.com/maxsharlaev/autoreview-bot/issues/9)): one deployment can
  serve several organizations or accounts through an optional `owners:` block. Each owner has its own GitHub
  App or PAT, webhook secret (`/api/v1/pull-request` checks all, `/api/v1/pull-request/<id>` one), scoped API
  key, Jira site and projects, Slack channel and digest, policy overrides (`public_repos`, `language`,
  `features`, `pr_description`, `size_guard`) and model settings (`codex.model`, `reasoning_effort`,
  `api_key_env`). Repositories are routed by explicit owner, `allowed_repos` (exact or `org/*`), installation
  id and `routing.unclaimed`. Integrations are never inherited between owners. API responses gain `owner`.
- Migrations `005_owner_id`, `006_comment_authors_objects` and `007_findings_owner_id` add `owner_id` to
  repositories, review runs, digest runs and findings, and store comment authors per owner. Downgrading `007`
  with findings of several owners on one PR requires `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1`.
- A config without `owners:` behaves as before (owner `default`). See the README section "Multiple owners".

### Changed

- **Webhook authentication**: `POST /api/v1/pull-request` now requires only a
  valid `X-Hub-Signature-256` HMAC signature; the API key (`REVIEW_API_KEY`) is
  no longer checked on this endpoint. This allows standard GitHub webhooks
  (from repository settings or a GitHub App) to work without custom headers.
- **API key for /api/v1/reviews**: The manual/Actions endpoint continues to
  require `REVIEW_API_KEY` for authentication.
- **Webhook secret validation**: If `GITHUB_WEBHOOK_SECRET` is not configured
  (empty, a placeholder like `change-me`, or shorter than 16 characters), the
  webhook endpoint is disabled (returns 503); `/api/v1/reviews` remains available.
- A startup warning is now logged when `github.allowed_repos` is empty, as this
  configuration accepts webhook requests from any repository.
