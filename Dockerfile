FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Pinned Bruno CLI version: verify against https://www.npmjs.com/package/@usebruno/cli before bumping.
ARG BRUNO_CLI_VERSION=4.2.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g "@usebruno/cli@${BRUNO_CLI_VERSION}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# --frozen: the lockfile is the single source of truth for reproducible builds.
RUN uv sync --no-dev --frozen

RUN useradd --create-home --uid 10001 bruno \
    && mkdir -p /app/build \
    && chown -R bruno:bruno /app/build

USER bruno

CMD ["/app/.venv/bin/bruno-mcp"]
