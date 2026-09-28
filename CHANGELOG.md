# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
