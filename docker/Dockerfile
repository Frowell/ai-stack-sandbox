# Microsoft devcontainer base = sane Python + non-root `vscode` user + git.
FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# The base image ships a Yarn apt repo whose signing key periodically expires,
# which breaks `apt-get update` (and any devcontainer Feature that calls it). We
# don't use Yarn, so drop the repo to keep apt usable if you add packages later.
RUN rm -f /etc/apt/sources.list.d/yarn.list

# uv: fast, reproducible dependency management.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/workspace/.venv

WORKDIR /workspace

# Dependencies are installed by `uv sync` in postCreateCommand so the lockfile
# stays the source of truth and rebuilds are cache-friendly.
CMD ["sleep", "infinity"]
