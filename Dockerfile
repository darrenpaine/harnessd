FROM python:3.15-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /srv
ENV \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/srv/.venv/bin:$PATH"

# Install dependencies: this layer is cached until the lockfile changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ ./

# Run as an unprivileged user (threat model T-32). The ids match the host user
# by default, so the notes folder mounted by compose stays writable; set
# APP_UID / APP_GID in .env if yours differ.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /srv/data/notes \
    && chown -R app:app /srv/data
USER app

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5999"]
