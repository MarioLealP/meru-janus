FROM docker.io/library/python:3.15.2-slim@sha256:c4158543ecb725b99e57d129dfd43d761d4c43e4577b5e23326aabb223cf7f4e

COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /bin/uv

# git: Janus opens request PRs by AGit push (refs/for/main) - Gitea's API can't do that with Read alone.
RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
# the dependencies only: the image runs the code from src/ (the package build is for the janus command line)
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/

# The root filesystem is read-only in Meru, so no .pyc files; the numeric user matches nuwa-app's runAsUser.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8080
CMD [".venv/bin/uvicorn", "--app-dir", "src", "janus.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
