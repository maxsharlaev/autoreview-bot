# Upgrade and rollback

There are no versioned releases yet; deployments track `main`. Notable changes are listed in [CHANGELOG.md](../CHANGELOG.md).

## Upgrading a Compose deployment

1. Read the `Unreleased` section of the changelog for new permissions, keys or migrations.
2. Back up the Postgres volume (`postgres_data`), for example with `pg_dump` from the `postgres` container.
3. Update the checkout (`git pull`) and rebuild: `docker compose up -d --build`.
4. Compose starts `migrate` first (`alembic upgrade head`, one shot); `api` and `worker` start only after it exits successfully. Check `docker compose logs migrate`.
5. Check `GET /health`, `GET /is-ready`, the API startup log (owners, default owner, config warnings) and `GET /api/v1/ops/status`.

Without Compose: stop the worker, run `alembic upgrade head` with the new code and the production `DATABASE_URL`, then start the API and worker.

Configuration stays compatible: new keys have defaults that keep the previous behaviour, and unknown top-level YAML keys are ignored. Restart the API and worker after changing `config.yaml` or `.env`.

## Migrations

| Revision | Change | Notes |
| --- | --- | --- |
| `001_initial` | Repositories, pull requests, review runs, findings, transitions, task snapshots, digest runs. | |
| `002_bot_title` | Bot-owned PR titles and their source hash. | Needed for `pr_description.title_mode`. |
| `003_comment_authors` | GitHub identities that published sticky comments. | Run the new version once with the old credentials before switching between PAT and App, so older comments stay recognized. |
| `004_finding_details_visibility` | Where finding details were created (private/public). | Last revision before multi-owner support. |
| `005_owner_id` | `owner_id` on repositories, review runs, digest runs (default `default`). | |
| `006_comment_authors_objects` | Comment authors stored as `{login, owner_id, kind}` objects. | |
| `007_findings_owner_id` | `owner_id` on findings, required. | Downgrade may need `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1` (below). |
| `008_review_run_progress` | `progress_status`, `comment_url`, `has_sensitive_content` on review runs. | NULL for existing rows. |

## Rollback

- **Configuration only** (for example an owner block): restore the previous `config.yaml` / `.env` and recreate the API and worker. Runs queued for a removed owner are skipped as `owner_not_configured`, for a disabled one as `owner_disabled`; their rows stay. See [MULTI_OWNER.md](MULTI_OWNER.md#upgrade-and-rollback) for the order that also closes progress statuses.
- **Code with the same schema:** check out the previous commit and rebuild.
- **Code with an older schema:** downgrade **with the new image first**, then deploy the old one: `docker compose run --rm migrate alembic downgrade <revision>`. Going back to an image from before multi-owner support needs `alembic downgrade 004_finding_details_visibility`; the old image cannot use the upgraded schema (after `007` `findings.owner_id` is required without a default, after `006` comment authors are objects).
- The `007_findings_owner_id` downgrade refuses, and reports the count, while findings of several owners share a stable id on one pull request. With `MIGRATION_007_DOWNGRADE_DROP_DUPLICATES=1` it keeps the repository owner's row (or the oldest) and deletes the others with their transitions.

Always back up the Postgres volume before a downgrade.

## Moving to several owners

Step-by-step upgrade to an `owners:` block, keeping history with `aliases: [default]`, and rollback: [MULTI_OWNER.md](MULTI_OWNER.md#upgrade-and-rollback).

## Worker metrics port no longer published

The Compose file no longer maps the worker's metrics port to `127.0.0.1:9100` on the host; it is exposed only on the Docker network, so `docker compose up --scale worker=N` works. If your Prometheus scraped `127.0.0.1:9100` / `localhost:9100`, switch it to the Docker network: attach Prometheus to the stack's network and use the bundled `dns_sd_configs` job from [`observability/prometheus-scrape.yml`](../observability/prometheus-scrape.yml), which scrapes every replica. To keep a host port for a single worker, add a `docker-compose.override.yml` with `ports: ["127.0.0.1:9100:9100"]` on `worker` (then it can't be scaled). Reload `observability/alerts.yml` too: `OpenPrReviewWorkerDown` (critical) now fires only when no worker replica is discovered, and the new `OpenPrReviewWorkerTargetDown` (warning) fires when a discovered replica cannot be scraped. Update alert routing if it matched `OpenPrReviewWorkerDown` for single unreachable replicas.

## New GitHub permissions

Progress status (from `008_review_run_progress`) uses **Checks: Read and write** (App) or **Commit statuses: Read and write** (PAT). Without them reviews keep working without a status ([PROGRESS_STATUS.md](PROGRESS_STATUS.md)).
