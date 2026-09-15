#!/usr/bin/env bash
#
# Build the shared image that carries uv and a warmed package cache.
#
# Every baked task image copies uv and the cache from this image rather than
# installing them, which is what makes grading network-free.
#
#     scripts/build-uv-base.sh
#
# Why a shared base image instead of a per-task layer:
#
#   * `apt-get install curl` is not dependable. Several Terminal-Bench task
#     images ship apt sources that no longer resolve, so such a layer fails with
#     "Unable to locate package curl" -- and a transient outage fails a working
#     image the same way, indistinguishably.
#   * `curl | sh` from astral.sh is the download the verifiers were failing on:
#     SSL_ERROR_SYSCALL under load, connection reset on a bad node.
#   * putting the ~700 MB bundle in each task's build context made the context
#     2.5 GB. Copying from an image keeps it at a few kilobytes.

set -euo pipefail

UV_VERSION="${UV_VERSION:-0.9.5}"
IMAGE="${UV_BASE_IMAGE:-xh-uv-base:${UV_VERSION}}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> fetching uv ${UV_VERSION}"
curl -fsSL -o "$WORK/uv.tar.gz" \
  "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz"
tar -xzf "$WORK/uv.tar.gz" -C "$WORK" --strip-components=1
install -m 0755 "$WORK/uv" "$WORK/uvx" "$WORK/"

echo "==> warming the cache"
# One task at a time rather than the union of all requirements: the union is
# unsatisfiable, because different tasks pin different versions of the same
# package (selenium 4.35.0 and 4.38.0, four numpy versions). uv's cache is
# content-addressed, so running each task's exact spec in turn accumulates every
# version in one directory.
export UV_CACHE_DIR="$WORK/cache"
export UV_PYTHON_INSTALL_DIR="$WORK/python"
mkdir -p "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR"

if [[ -d "${TASKS_DIR:-tasks-baked}" ]]; then
  while IFS= read -r script; do
    spec="$(python3 - "$script" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / "src"))
from xharness_bench.prepare_tasks import parse_uvx
s = parse_uvx(Path(sys.argv[1]).read_text(errors="replace"))
if s.empty:
    raise SystemExit
parts = []
if s.python:
    parts += ["-p", s.python]
for w in s.packages:
    parts += ["-w", w]
print(" ".join(parts))
PY
)" || continue
    [[ -z "$spec" ]] && continue
    # shellcheck disable=SC2086
    timeout 1800 "$WORK/uvx" $spec pytest --version >/dev/null 2>&1 \
      && echo "    ok  $(basename "$(dirname "$(dirname "$script")")")" \
      || echo "    skip $(basename "$(dirname "$(dirname "$script")")")"
  done < <(find "${TASKS_DIR:-tasks-baked}" -name test.sh -path '*/tests/*' | sort)
else
  # No task set on hand: warm the common verifier toolchain, which is what most
  # tasks need. Build-time `uvx` in each image can still reach the network for
  # anything the cache lacks.
  "$WORK/uvx" -p 3.13 -w pytest==8.4.1 -w pytest-json-ctrf==0.3.5 pytest --version >/dev/null
fi

echo "==> building ${IMAGE}"
cat > "$WORK/Dockerfile" <<'DOCKER'
FROM ubuntu:24.04
COPY uv uvx /usr/local/bin/
COPY cache /root/.cache/uv
COPY python /root/.local/share/uv/python
ENV UV_CACHE_DIR=/root/.cache/uv \
    UV_PYTHON_INSTALL_DIR=/root/.local/share/uv/python
RUN chmod 0755 /usr/local/bin/uv /usr/local/bin/uvx \
 && uv --version && uvx --version
DOCKER
docker build -t "$IMAGE" "$WORK"
echo "==> done: ${IMAGE} ($(docker images --format '{{.Size}}' "$IMAGE"))"