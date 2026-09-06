FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.14-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --locked --no-dev

FROM python:3.14-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN addgroup --system eva && adduser --system --ingroup eva eva
COPY --from=builder --chown=eva:eva /app /app

USER eva
EXPOSE 8080
CMD ["uvicorn", "eva_ai.main:app", "--host", "0.0.0.0", "--port", "8080"]
