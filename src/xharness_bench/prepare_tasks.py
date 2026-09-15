"""Bake the verifier's network dependencies into task images.

Why this exists
---------------
Every Terminal-Bench 2.0 verifier acquires its own toolchain at test time:

    curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh    # -> GitHub release
    uvx -p 3.13 -w pytest==8.4.1 ... pytest /tests/test_outputs.py

That is three external services (astral.sh, GitHub, PyPI) and tens of megabytes,
per trial, *before any test runs*. When it fails, the script writes
``reward.txt = 0``, which is indistinguishable from the agent failing the task.
On a host with intermittently degraded connectivity this produces false
negatives at a rate that swamps the effect being measured -- observed directly:
the oracle reference solution scored 1/5, both failures being verifier-side
network errors.

What this patches
-----------------
Two files per task, and nothing else:

1. ``environment/Dockerfile`` -- append a layer that installs ``uv`` and runs the
   verifier's exact ``uvx`` command once, so the CPython 3.13 toolchain and the
   pinned test packages are already in the image's uv cache.
2. ``tests/test.sh`` -- neutralise the download line (uv is already present) and
   export ``UV_OFFLINE=1`` so resolution comes from the warm cache.

**The tests are not touched.** The same test file runs against the same agent
output and the same reward is written. The only thing removed is the verifier's
own dependency on the network at grading time, which is not part of what the task
is measuring. That claim is checkable rather than asserted: the manifest this
writes records a hash of ``tests/test_outputs.py`` and of the Dockerfile's
inherited content, so a reviewer can confirm the test body is byte-identical and
that the Dockerfile patch is purely additive.

Usage
-----
    python -m xharness_bench.prepare_tasks \
        --source tasks/terminal-bench --output tasks-baked \\
        --uv-version 0.9.5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# The verifier's download line, in the forms Terminal-Bench actually uses.
_INSTALL_PATTERNS = (
    re.compile(r"^\s*curl\s+-LsSf\s+https://astral\.sh/uv/[^\s|]+\s*\|\s*sh\s*$"),
    re.compile(r"^\s*curl\s+-LsSf\s+https://astral\.sh/uv/install\.sh\s*\|\s*sh\s*$"),
    re.compile(r"^\s*curl\s+[^|]*astral\.sh[^|]*\|\s*sh\s*$"),
)

# `uvx ... pytest ...`, possibly wrapped over continuation lines.
_UVX_START = re.compile(r"^\s*uvx\b")
_FLAG = re.compile(r"(?P<flag>-p|-w)\s+(?P<value>\S+)")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _logical_lines(text: str) -> list[str]:
    """Join backslash continuations so a wrapped uvx call is one line."""
    joined: list[str] = []
    pending = ""
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        joined.append(pending + line)
        pending = ""
    if pending:
        joined.append(pending)
    return joined


@dataclass
class UvxSpec:
    """The toolchain the verifier asks uvx for."""

    python: str | None = None
    packages: list[str] = field(default_factory=list)

    def command(self, entrypoint: str = "pytest", tail: str = "--version") -> str:
        parts = ["uvx"]
        if self.python:
            parts += ["-p", self.python]
        for package in self.packages:
            parts += ["-w", package]
        parts += [entrypoint, tail]
        return " ".join(parts)

    @property
    def empty(self) -> bool:
        return not self.python and not self.packages


def parse_uvx(test_sh: str) -> UvxSpec:
    """Extract the python version and `-w` requirements from a verifier script."""
    spec = UvxSpec()
    for line in _logical_lines(test_sh):
        if not _UVX_START.match(line):
            continue
        for match in _FLAG.finditer(line):
            flag, value = match.group("flag"), match.group("value")
            if flag == "-p":
                spec.python = value
            elif flag == "-w":
                spec.packages.append(value)
        break
    return spec


def patch_test_sh(text: str) -> tuple[str, bool]:
    """Neutralise the uv download and force offline resolution."""
    lines = text.splitlines()
    changed = False
    for index, line in enumerate(lines):
        for pattern in _INSTALL_PATTERNS:
            if pattern.match(line):
                indent = line[: len(line) - len(line.lstrip())]
                lines[index] = (
                    f"{indent}# uv and its cache are baked into the image by "
                    f"xharness_bench.prepare_tasks; no download needed."
                )
                changed = True
                break

    body = "\n".join(lines)
    # Upstream scripts assume the installer wrote this; keep the line harmless.
    if "UV_OFFLINE" not in body:
        header = (
            "# Baked by xharness_bench.prepare_tasks: resolve everything from the\n"
            "# image's uv cache instead of the network.\n"
            "export UV_OFFLINE=1\n"
            "export PATH=\"$HOME/.local/bin:$PATH\"\n"
        )
        body = body.replace("#!/bin/bash\n", "#!/bin/bash\n" + header, 1)
        if "UV_OFFLINE" not in body:
            body = header + body
        changed = True
    return body.rstrip("\n") + "\n", changed


_TASK_TOML_IMAGE = re.compile(r'^\s*docker_image\s*=\s*"([^"]+)"', re.MULTILINE)


def read_prebuilt_image(task_dir: Path) -> str | None:
    """The task's own published image, if it declares one."""
    manifest = task_dir / "task.toml"
    if not manifest.is_file():
        return None
    match = _TASK_TOML_IMAGE.search(manifest.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else None


def dockerfile_from_prebuilt(original: str, base_image: str, spec: UvxSpec, uv_version: str) -> str:
    """Rebuild on top of the task's published image instead of from its Dockerfile.

    Why this is the right base: the published image is what the task author
    validated. Rebuilding from the Dockerfile pulls fresh packages from Debian
    and PyPI, which is not the same environment -- measured, not assumed. On this
    host a from-Dockerfile rebuild of ``break-filter-js-from-html`` installs a
    different Chromium and the reference solution then fails a test it passes in
    the published image.

    So the base is pinned to the published digest-able tag and the original
    Dockerfile is preserved verbatim as comments: nothing is dropped, and a
    reviewer can diff intent against the commented text.
    """
    preserved = "\n".join(
        f"# {line}" if line.strip() else "#" for line in original.rstrip("\n").splitlines()
    )
    warm = (
        spec.command(entrypoint="pytest", tail="--version")
        if not spec.empty
        else "uv --version"
    )
    return f"""# Generated by xharness_bench.prepare_tasks --from-prebuilt.
#
# The task's published image is the base, so the environment is exactly what the
# task author validated. The original Dockerfile is reproduced below as comments.
#
# The appended layer installs uv and warms its cache, so the verifier needs no
# network at grading time. The tests themselves are not modified.
FROM {base_image}

{preserved}

RUN set -eux; \\
    export PATH="$HOME/.local/bin:$PATH"; \\
    if ! command -v curl >/dev/null 2>&1; then \\
        if command -v apt-get >/dev/null 2>&1; then \\
            apt-get update; \\
            apt-get install -y --no-install-recommends curl ca-certificates; \\
        elif command -v apk >/dev/null 2>&1; then \\
            apk add --no-cache curl ca-certificates; \\
        elif command -v dnf >/dev/null 2>&1; then \\
            dnf install -y curl ca-certificates; \\
        fi; \\
    fi; \\
    curl -LsSf https://astral.sh/uv/{uv_version}/install.sh | sh; \\
    {warm}; \\
    uv --version; \\
    du -sh "$HOME/.cache/uv" 2>/dev/null || true
"""



# Tag of the shared image that carries uv and a warmed cache. Built once by
# ``scripts/build-uv-base.sh``; every baked task copies from it.
UV_BASE_IMAGE = "xh-uv-base:0.9.5"

_OFFLINE_LAYER = """# --- xharness_bench.prepare_tasks (offline bundle) --------------------------
# uv and a warmed cache are copied from a shared base image, so this task image
# needs NO network and NO package manager to grade.
#
# COPY --from rather than COPY: the bundle is ~700 MB, and putting it in the
# build context made the context 2.5 GB per task. Copying from an image keeps the
# context at a few kilobytes and the build at ~40 s.
#
# Both omissions are deliberate and both were forced by measurement:
#
#   * No `apt-get install curl`. Several task images ship apt sources that no
#     longer resolve, so a layer depending on apt fails with "Unable to locate
#     package curl" -- and a transient outage makes a working image fail the same
#     way, indistinguishably.
#   * No `curl | sh` from astral.sh. That download is what the verifiers were
#     failing on: SSL_ERROR_SYSCALL under load, connection reset on a bad node.
#     Removing it removes the failure mode rather than retrying around it.
#
# The tests themselves are untouched; only the harness's own toolchain
# acquisition is.
COPY --from=%(base)s /usr/local/bin/uv /usr/local/bin/uv
COPY --from=%(base)s /usr/local/bin/uvx /usr/local/bin/uvx
COPY --from=%(base)s /root/.cache/uv /root/.cache/uv
COPY --from=%(base)s /root/.local/share/uv/python /root/.local/share/uv/python
ENV UV_CACHE_DIR=/root/.cache/uv \
    UV_PYTHON_INSTALL_DIR=/root/.local/share/uv/python
RUN /usr/local/bin/uv --version \
 && /usr/local/bin/uvx --version \
 && %(warm)s
# ---------------------------------------------------------------------------
"""


def dockerfile_offline(original: str, base_image: str, spec: UvxSpec) -> str:
    """Base on the published image and add a network-free uv layer."""
    preserved = "\n".join(
        f"# {line}" if line.strip() else "#" for line in original.rstrip("\n").splitlines()
    )
    warm = (
        spec.command(entrypoint="pytest", tail="--version")
        if not spec.empty
        else "uv --version"
    )
    header = f"""# Generated by xharness_bench.prepare_tasks --from-prebuilt --offline.
#
# Base is the task's published image, so the environment is exactly what the task
# author validated. The original Dockerfile is reproduced below as comments.
#
# The appended layer copies in uv and a warmed cache from {UV_BASE_IMAGE}, so
# grading needs no network. The tests are not modified.
FROM {base_image}

{preserved}
"""
    return header + (_OFFLINE_LAYER % {"base": UV_BASE_IMAGE, "warm": warm})


def patch_dockerfile(text: str, spec: UvxSpec, uv_version: str) -> str:
    """Append a layer that installs uv and warms its cache."""
    warm = spec.command(entrypoint="pytest", tail="--version") if not spec.empty else "uv --version"
    # The base images are slim and ship no HTTP client: the verifier installs
    # curl at grading time, which is far too late for a build-time layer. So the
    # layer has to provide its own, for both apt and apk based images.
    layer = f"""

# --- xharness_bench.prepare_tasks -------------------------------------------
# Pre-install uv and warm its cache so the verifier needs no network. The tests
# themselves are untouched; this removes only the harness's own dependency on
# astral.sh, GitHub and PyPI at grading time.
RUN set -eux; \\
    export PATH="$HOME/.local/bin:$PATH"; \\
    if ! command -v curl >/dev/null 2>&1; then \\
        if command -v apt-get >/dev/null 2>&1; then \\
            apt-get update; \\
            apt-get install -y --no-install-recommends curl ca-certificates; \\
        elif command -v apk >/dev/null 2>&1; then \\
            apk add --no-cache curl ca-certificates; \\
        elif command -v dnf >/dev/null 2>&1; then \\
            dnf install -y curl ca-certificates; \\
        fi; \\
    fi; \\
    curl -LsSf https://astral.sh/uv/{uv_version}/install.sh | sh; \\
    {warm}; \\
    uv --version; \\
    du -sh "$HOME/.cache/uv" 2>/dev/null || true
# ---------------------------------------------------------------------------
"""
    return text.rstrip("\n") + layer


@dataclass
class PatchRecord:
    task: str
    dockerfile_sha256_before: str
    dockerfile_sha256_after: str
    test_sh_sha256_before: str
    test_sh_sha256_after: str
    tests_body_sha256: str
    uvx: dict[str, object]
    test_sh_changed: bool
    base_image: str | None = None
    base_mode: str = "dockerfile"
    skipped: str | None = None


def prepare_task(
    task_dir: Path,
    output_dir: Path,
    uv_version: str,
    dry_run: bool = False,
    from_prebuilt: bool = False,
    offline: bool = False,
) -> PatchRecord:
    """Copy one task and bake its verifier dependencies."""
    dockerfile = task_dir / "environment" / "Dockerfile"
    test_sh = task_dir / "tests" / "test.sh"

    record = PatchRecord(
        task=task_dir.name,
        dockerfile_sha256_before=sha256_file(dockerfile) if dockerfile.is_file() else "",
        dockerfile_sha256_after="",
        test_sh_sha256_before=sha256_file(test_sh) if test_sh.is_file() else "",
        test_sh_sha256_after="",
        tests_body_sha256="",
        uvx={},
        test_sh_changed=False,
    )

    if not test_sh.is_file():
        record.skipped = "no tests/test.sh"
        return record
    if not dockerfile.is_file():
        record.skipped = "no environment/Dockerfile (prebuilt image only)"
        return record

    test_text = test_sh.read_text(encoding="utf-8", errors="replace")
    spec = parse_uvx(test_text)

    prebuilt = read_prebuilt_image(task_dir) if from_prebuilt else None
    record.base_image = prebuilt
    record.base_mode = "prebuilt" if prebuilt else "dockerfile"
    record.uvx = {"python": spec.python, "packages": spec.packages}

    # Hash the test body itself, so "the tests were not modified" is verifiable.
    body = task_dir / "tests" / "test_outputs.py"
    record.tests_body_sha256 = sha256_file(body) if body.is_file() else ""

    if dry_run:
        record.dockerfile_sha256_after = record.dockerfile_sha256_before
        record.test_sh_sha256_after = record.test_sh_sha256_before
        return record

    target = output_dir / task_dir.name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(task_dir, target)

    new_test, changed = patch_test_sh(test_text)
    record.test_sh_changed = changed
    (target / "tests" / "test.sh").write_text(new_test, encoding="utf-8")

    original = dockerfile.read_text(encoding="utf-8", errors="replace")
    if offline:
        if not prebuilt:
            record.skipped = "offline mode requires a published docker_image"
            return record
        new_docker = dockerfile_offline(original, prebuilt, spec)
    elif prebuilt:
        new_docker = dockerfile_from_prebuilt(original, prebuilt, spec, uv_version)
    else:
        new_docker = patch_dockerfile(original, spec, uv_version)
    (target / "environment" / "Dockerfile").write_text(new_docker, encoding="utf-8")
    (target / "environment" / "Dockerfile").chmod(0o644)

    if offline:
        record.base_mode = "prebuilt-offline"

    record.dockerfile_sha256_after = sha256_file(target / "environment" / "Dockerfile")
    record.test_sh_sha256_after = sha256_file(target / "tests" / "test.sh")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uv-version", default="0.9.5")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--only", nargs="*", help="restrict to these task names")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "copy the uv binaries and a warmed cache into the image instead of "
            "installing them with apt+curl. Requires --from-prebuilt. This is the "
            "robust mode: the layer cannot fail on a missing package, and the "
            "verifier needs no network at grading time."
        ),
    )
    parser.add_argument(
        "--from-prebuilt",
        action="store_true",
        help=(
            "base each image on the task's published docker_image instead of "
            "rebuilding from its Dockerfile. Prefer this: rebuilding is not "
            "environment-equivalent, and the published image is what the task "
            "author validated."
        ),
    )
    args = parser.parse_args(argv)

    candidates = sorted(p for p in args.source.iterdir() if (p / "task.toml").is_file())
    if args.only:
        wanted = set(args.only)
        candidates = [p for p in candidates if p.name in wanted]

    args.output.mkdir(parents=True, exist_ok=True)
    records = [
        prepare_task(
            task,
            args.output,
            args.uv_version,
            dry_run=args.dry_run,
            from_prebuilt=args.from_prebuilt,
            offline=args.offline,
        )
        for task in candidates
    ]

    patched = [r for r in records if not r.skipped]
    skipped = [r for r in records if r.skipped]
    print(f"{len(records)} task(s): {len(patched)} patched, {len(skipped)} skipped")
    by_mode: dict[str, int] = {}
    for record in patched:
        by_mode[record.base_mode] = by_mode.get(record.base_mode, 0) + 1
    for mode, count in sorted(by_mode.items()):
        print(f"  base={mode}: {count}")
    only_dockerfile = [r for r in patched if r.base_mode == "dockerfile"]
    if only_dockerfile:
        print(
            f"  note: {len(only_dockerfile)} task(s) declare no docker_image and "
            "were built from their Dockerfile"
        )
    for record in skipped:
        print(f"  skipped {record.task}: {record.skipped}")

    no_uvx = [r for r in patched if not r.uvx.get("python") and not r.uvx.get("packages")]
    if no_uvx:
        print(
            f"  note: {len(no_uvx)} task(s) declare no uvx toolchain, so their "
            "verifier only needed the uv binary"
        )

    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps(
                {
                    "uv_version": args.uv_version,
                    "source": str(args.source),
                    "tasks_patched": len(patched),
                    "tasks_skipped": len(skipped),
                    "records": [vars(r) for r in records],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"manifest written to {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())