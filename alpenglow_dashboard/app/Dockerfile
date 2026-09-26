# ── stage 1: frontend build ───────────────────────────────────────────────────
FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── stage 2: python runtime ───────────────────────────────────────────────────
FROM python:3.13-slim

# docker CLI + compose plugin — the app drives the *host* daemon through the
# mounted /var/run/docker.sock; no daemon runs in this container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    STATIC_DIR=/app/static \
    # OpenTelemetry auto-instrumentation is wired in but exports nothing
    # unless compose points it at a collector (OTEL_TRACES_EXPORTER=otlp).
    OTEL_TRACES_EXPORTER=none \
    OTEL_METRICS_EXPORTER=none \
    OTEL_LOGS_EXPORTER=none \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_PYTHON_FASTAPI_EXCLUDE_SPANS=receive,send

WORKDIR /app/backend

# Dependency layer first for cache-friendly rebuilds.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/ ./
RUN uv sync --frozen --no-dev

COPY --from=frontend /build/dist /app/static

EXPOSE 8080
CMD ["uv", "run", "--no-sync", "opentelemetry-instrument", "uvicorn", "alpenglow_dashboard.main:app", "--host", "0.0.0.0", "--port", "8080"]
