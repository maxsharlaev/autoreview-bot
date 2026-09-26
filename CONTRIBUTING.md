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
# All tests
pytest

# Linting
ruff check .

# Formatting check
ruff format --check .

# Fix formatting
ruff format .
```

CI runs these checks on every push and pull request. Please ensure they pass locally before submitting.

## Pull request guidelines

1. **Create a branch** from `main` with a descriptive name.
2. **Keep changes focused** — one logical change per PR.
3. **Add tests** for new functionality or bug fixes when feasible.
4. **Update documentation** if behavior changes.
5. **Ensure CI passes** — the PR must pass `ruff check`, `ruff format --check`, `pytest`, and the Docker image build.

## Code style

- Follow existing patterns in the codebase.
- Use type hints for function signatures.
- Keep lines under 120 characters (configured in `pyproject.toml`).
- Ruff handles linting and formatting — run `ruff format .` before committing.

## Testing

Tests live in the `tests/` directory. Use `pytest` with the async fixtures already configured:

```bash
pytest tests/                    # Run all tests
pytest tests/test_health.py      # Run specific test file
pytest -v                        # Verbose output
```

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 license.
