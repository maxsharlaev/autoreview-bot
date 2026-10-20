# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Review progress status** (#7): webhook runs report progress on the PR head
  as an `autoreview` check run (GitHub App, needs Checks: Read and write) or
  commit status (PAT, needs Commit statuses: Read and write). On by default
  (`progress_status.enabled: true`, `mode: auto`); updates are best effort, so
  deployments without the permission keep working. Findings never produce a
  failing check.
- Migration `008_review_run_progress`: `review_runs.progress_status`,
  `comment_url` and `has_sensitive_content` (NULL for existing rows).

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
