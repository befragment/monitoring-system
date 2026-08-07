FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Зависимости ставим первыми, чтобы слой кэшировался, пока меняются только исходники.
# Группа dev (alembic, psycopg2-binary) попадает и в рантайм-образ: сервис
# migrate переиспользует его для запуска `alembic upgrade head`.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system app && useradd --system --gid app app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY --chown=app:app src ./src
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations

USER app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
