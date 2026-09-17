#!/usr/bin/env bash
# Cache the wheels a container will need, so a run can provision offline.
#
# Why this exists: task containers cannot reliably reach PyPI. Setup installs with
# `uv pip install --offline` from a bind-mounted cache, which is deterministic -- but
# it means an uncached version fails outright. That is the right failure, since a run
# labelled "latest" must not silently provision something older. It just has to be
# cheap to avoid, which is what this script is for.
#
# Run it after choosing a version, before the run that uses it:
#
#     scripts/warm-sdk-cache.sh 0.1.5rc1
#     scripts/warm-sdk-cache.sh latest
#
# "latest" is resolved by calling the adapter's own resolve_sdk_version, not by a
# second copy of the rule. The first version of this script reimplemented the
# ordering inline and returned an empty string on a machine whose python3 has no
# packaging -- a duplicate rule drifting from the original within the hour.
#
# Needs network. Uses the host's uv, not the container's.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${UV_CACHE_DIR:-$HOME/uvwarm/cache}"
UV_BIN_DIR="${UV_BIN_DIR:-$HOME/uvbin}"
BENCH_PYTHON="${BENCH_PYTHON:-$HOME/bench-venv/bin/python}"
PACKAGE="deepseek-harness-sdk"

export PATH="$UV_BIN_DIR:$PATH"
export UV_CACHE_DIR="$CACHE"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found on PATH (looked in $UV_BIN_DIR)" >&2
  exit 1
fi

want="${1:-}"
if [ -z "$want" ]; then
  echo "usage: $0 <version|latest>" >&2
  exit 2
fi

if [ "$want" = "latest" ]; then
  want="$(cd "$HERE" && "$BENCH_PYTHON" -c '
import sys
sys.path.insert(0, "src")
from xharness_bench.agents.dsh_upstream import LATEST, resolve_sdk_version
print(resolve_sdk_version(LATEST))
' 2>/dev/null)" || true
  if [ -z "$want" ]; then
    echo "could not resolve 'latest' via $BENCH_PYTHON" >&2
    echo "pass an explicit version instead" >&2
    exit 1
  fi
  echo "latest resolves to $want"
fi

echo "caching $PACKAGE==$want into $CACHE"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
uv venv --python 3.12 "$scratch/venv" >/dev/null 2>&1
uv pip install --python "$scratch/venv/bin/python" "$PACKAGE==$want" || {
  echo "failed to cache $PACKAGE==$want" >&2
  exit 1
}

echo
echo "cached. verifying it now installs with the network off:"
uv venv --python 3.12 "$scratch/verify" >/dev/null 2>&1
if uv pip install --offline --python "$scratch/verify/bin/python" "$PACKAGE==$want" >/dev/null 2>&1; then
  echo "  OK -- $PACKAGE==$want provisions offline"
else
  echo "  FAILED -- it cached but will not install offline; a container will die in setup" >&2
  exit 1
fi