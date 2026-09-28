# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Webhook signature (`X-Hub-Signature-256`) is now required on every request to
  `POST /api/v1/pull-request`. The `GITHUB_WEBHOOK_SECRET` environment variable
  must be configured; the service will not start without it.
- A startup warning is now logged when `github.allowed_repos` is empty, as this
  configuration accepts webhook requests from any repository.
