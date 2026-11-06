#!/usr/bin/env bash
# Every check CI runs, runnable locally: lint, format, types, file size, TLS checks, the lockfile, pip-audit, tests.
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$*"; }

step ruff
uv run ruff check .
uv run ruff format --check .
step ty
uv run ty check
step "no source file over 500 lines"
too_long=$(find src -name '*.py' -exec awk 'FNR == 501 { print FILENAME }' {} +)
if [[ -n $too_long ]]; then
  printf '%s\n' "$too_long"
  exit 1
fi
step "layout: only __init__, __main__ and config at the top of src/<app>"
loose=$(find src -mindepth 2 -maxdepth 2 -name '*.py' ! -name __init__.py ! -name __main__.py ! -name config.py)
if [[ -n $loose ]]; then
  printf '%s\n' "$loose"
  echo "move these into a folder (STANDARDS.md, Layout)"
  exit 1
fi
step "TLS checks stay on"
# ruff's rule for this only knows requests and httpx by name, not httpx2.
if grep -rn 'verify=False' src; then
  exit 1
fi
step "uv.lock up to date"
uv lock --check
step pip-audit
uvx pip-audit --disable-pip --require-hashes --progress-spinner off -r <(uv export --frozen --no-emit-project --quiet)
step "pytest, 2 minutes at most"
timeout 120 uv run pytest -q
printf '\nall checks passed\n'
