# Multi-stage build for DigitalOcean App Platform.
# Slim runtime image; non-root; uvicorn binds the App Platform-injected $PORT.

# ── builder ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Build deps for asyncpg / cryptography wheels (most have wheels but keep gcc safe).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip wheel --wheel-dir /wheels .

# ── runtime ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /srv

COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels reorderos-backend && rm -rf /wheels

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

USER app

EXPOSE 8080

# App Platform sets $PORT. Falls back to 8080 locally.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/health/live" || exit 1

# DEFAULT command = PRODUCTION-COMPATIBLE: migrate, then serve. The live production app
# has NO PRE_DEPLOY migrate job (repo-spec validation in CI is NOT live-spec application),
# and its DATABASE_URL is the bound admin DB — so the API container itself must keep
# running `alembic upgrade head` before Uvicorn, exactly as it does today. Removing the
# migration from this default would leave production's schema behind and
# app.main._assert_schema_at_head() would (correctly) refuse to serve.
#
# Environments that cut over to the restricted runtime role (staging first) OVERRIDE this
# with a Uvicorn-only `run_command` on the api component and run migrations in a dedicated
# PRE_DEPLOY `migrate` job as the admin role — the restricted `reorderos_app` role has no
# CREATE, so the alembic step below would fail its DDL-capability preflight there.
# See .do/staging.app.yaml + docs/security/restricted-runtime-role-runbook.md.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
