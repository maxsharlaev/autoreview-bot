FROM python:3.12-slim-bookworm AS base

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPEN_PR_REVIEW_DATA_DIR=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY prompts ./prompts
COPY schemas ./schemas
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts/run-api.sh ./scripts/run-api.sh

RUN pip install --no-cache-dir .
RUN chmod +x ./scripts/run-api.sh

FROM base AS api
EXPOSE 8000
CMD ["sh", "./scripts/run-api.sh"]

FROM base AS worker

RUN apt-get update \
    && apt-get install -y --no-install-recommends git gnupg bubblewrap \
    && chmod u+s /usr/bin/bwrap \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @openai/codex@0.164.0 \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 9100
CMD ["arq", "app.workers.settings.WorkerSettings"]
