# Contributing to autoreview-bot

Contributions are welcome under the [Apache-2.0 license](LICENSE).

## Local development setup

### Prerequisites

- Python 3.12+
- Docker and Docker Compose
- Git

### Quick start (Docker)

```bash
cp .env.example .env
cp config.example.yaml config.yaml
# Fill POSTGRES_PASSWORD with a random value
docker compose up --build
```

The API listens on `http://localhost:8000`. Check health with `GET /health` and readiness with `GET /is-ready`.

### Local Python setup (without Docker for the app)

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

pip install -e ".[dev]"
```

You still need PostgreSQL and Redis running (via Docker or locally). Set `DATABASE_URL` and `REDIS_URL` in your environment or `.env`.

```bash
alembic upgrade head
uvicorn app.main:app --reload        # API
arq app.workers.settings.WorkerSettings   # Worker (separate terminal)
```

## Running tests and linters

```bash
ruff check .
ruff format --check .      # `ruff format .` to fix
pytest                     # database tests are skipped
```

### Tests with Postgres

Database and migration tests run only when `TEST_DATABASE_URL` points to a database whose name ends with `_test` (a guard against production databases). The tests migrate that database down to an empty schema and up again.

```bash
docker run -d --name review-test-db -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:16
docker exec review-test-db createdb -U postgres review_test
TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5432/review_test pytest -rs
```

CI runs the full suite without a database, then the database tests (`tests/test_migration_*.py` and `tests/test_*_db.py`, listed in [`.github/workflows/ci.yml`](.github/workflows/ci.yml)) against Postgres, then builds the Docker image. When you add a database test, add it to that list.

`tests/test_docs.py` checks that every configuration key and environment variable is documented in [docs/CONFIGURATION.md](docs/CONFIGURATION.md) / [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md), and that relative links and anchors in Markdown files resolve.

## Database migrations

- One Alembic revision per schema change in `alembic/versions/`, named `NNN_short_name.py` with `revision = "NNN_short_name"` and `down_revision` set to the previous one.
- Never edit a migration that is on `main`; add a new one.
- Write a working `downgrade()`. If it can lose data, refuse by default and require an explicit opt-in env variable (see `007_findings_owner_id`).
- New columns on existing tables need a server default or must be nullable.
- Add a migration test against Postgres and register it in CI.
- Document the revision in [docs/UPGRADE.md](docs/UPGRADE.md).

## Pull request guidelines

1. Branch from `main`: `feat/…`, `fix/…`, `docs/…`, `test/…`, `ci/…`.
2. Keep changes focused: one logical change per PR. Open a draft while it is in progress.
3. Write the PR title and commits in English, imperative mood (`Fix …`, `Add …`).
4. Add tests for new behaviour and bug fixes.
5. Update the docs when behaviour, configuration or env variables change: a new config key goes to `config.example.yaml` and [docs/CONFIGURATION.md](docs/CONFIGURATION.md), a new env variable to `.env.example` and [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md). English docs are primary; [docs/ru](docs/ru) is a mirror.
6. Add an entry under `Unreleased` in [CHANGELOG.md](CHANGELOG.md).
7. CI must pass: `ruff check`, `ruff format --check`, `pytest`, database tests and the Docker build.

Security issues: see [SECURITY.md](SECURITY.md), not public issues.

## Code style

- Follow existing patterns in the codebase.
- Use type hints for function signatures.
- Keep lines under 120 characters (configured in `pyproject.toml`).
- Treat PR and Jira content as untrusted: put it in an `<untrusted_…>` block and strip boundary tags with `strip_boundary_tags` (`app/services/untrusted.py`) before it reaches a prompt.

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 license.
