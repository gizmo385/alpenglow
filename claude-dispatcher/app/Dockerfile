FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY dispatcher/ ./dispatcher/
RUN uv sync --frozen --no-dev

EXPOSE 8080
CMD ["/app/.venv/bin/uvicorn", "dispatcher.main:app", "--host", "0.0.0.0", "--port", "8080"]
