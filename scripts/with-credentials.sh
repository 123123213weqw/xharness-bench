#!/usr/bin/env bash
#
# Run a command with the benchmark's credentials loaded, then exec it.
#
# Why this is a separate file: a secret passed on a command line is visible to
# every process on the host via `ps`, lands in shell history, and is one careless
# copy-paste away from a commit. Loading from a file that lives *outside* the
# repository keeps the secret in exactly one place, with 0600 permissions, and
# keeps every command in the docs free of it.
#
# The credential file is not created by this repository and is not expected to be
# in it. See docs/status.md for the expected shape:
#
#     DEEPSEEK_API_KEY=sk-...
#     DEEPSEEK_BASE_URL=https://api.deepseek.com
#
# Usage:
#
#     scripts/with-credentials.sh harbor run -p tasks-baked --agent oracle
#
# Override the location with XHARNESS_BENCH_CREDENTIALS.

set -euo pipefail

credentials="${XHARNESS_BENCH_CREDENTIALS:-$HOME/.config/xharness-bench/credentials.env}"

if [[ -f "$credentials" ]]; then
  # `set -a` so the sourced assignments are exported to the child process.
  # Harbor reads them from its own environment and forwards them into the task
  # container; nothing here assumes a particular variable name.
  set -a
  # shellcheck disable=SC1090
  . "$credentials"
  set +a
else
  echo "with-credentials: $credentials not found; running without credentials" >&2
fi

# Where the XHarness adapter caches the release bundle (the host binary plus the
# static assets). Explicit rather than defaulted so a run never silently falls back
# to downloading: a cache miss costs a slow network round trip and, on a host with
# flaky egress, fails with a URLError that looks like a broken adapter.
export XHARNESS_BENCH_CACHE="${XHARNESS_BENCH_CACHE:-$HOME/.cache/xharness-bench}"

exec "$@"