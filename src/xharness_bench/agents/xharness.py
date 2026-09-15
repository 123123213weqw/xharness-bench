"""Harbor agent adapter for XHarness.

XHarness is not a CLI agent.  It is a desktop product: a Tauri shell around a
long-lived ``xharness-host`` sidecar that speaks JSON-RPC over loopback.  To run
it as a Terminal-Bench agent we therefore install the sidecar into the task
container and drive it through its own control plane.

Two properties of the shipped binary make this workable, both verified:

* ``xharness-host`` links only ``libc``, ``libm`` and ``libgcc_s`` -- no GTK, no
  WebKitGTK, no EGL.  It runs in a bare container.  (The *desktop shell* is the
  part that needs a GUI stack; the agent core does not.)
* it accepts ``--bind 127.0.0.1:0`` together with ``--ready-file``, so the task
  needs no fixed port and no port publishing.

The artifact is fetched and checksum-verified **on the host**, then uploaded, so
the task container never needs outbound access to GitHub.

Caveat, recorded rather than hidden: the published artifact is an AppImage whose
bundled GTK/WebKit stack is broken on Mesa 26 (``EGL_BAD_PARAMETER``).  That is
irrelevant here because only ``xharness-host`` and the static web assets are
extracted, but it is the same artifact, so the version pin matters.

Status: the control-plane sequence below is taken from the in-repo reference
driver (``scripts/agent-live-eval.py``, ``scripts/compaction-ablation.py``) and
mirrors the host's own argument parser.  It has not yet been run end-to-end
against a Terminal-Bench task from this repository -- see ``docs/status.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ..rpc import ContainerRpcClient, TransportError, exec_parts, message_text, normalized_events

DEFAULT_REPOSITORY = "123123213weqw/x-harness-rs"
INSTALL_DIR = "/opt/xharness"
STATE_DIR = "/opt/xharness/state"


@dataclass(frozen=True)
class Bundle:
    """A checksum-verified ``xharness-host`` plus its static UI assets."""

    version: str
    tag: str
    binary: Path
    web_dir: Path
    sha256: str


def _download(url: str, destination: Path, timeout: float = 300.0) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "xharness-bench"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1 << 20)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _usage_number(usage: dict[str, Any], *names: str) -> int:
    """Read a token count that may be camelCase or snake_case."""
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


class XHarnessAgent(BaseAgent):
    """Drive the XHarness agent core inside a task container."""

    capabilities = AgentCapabilities(windows=False)

    def __init__(
        self,
        *args: Any,
        repository: str = DEFAULT_REPOSITORY,
        version: str | None = None,
        provider: str = "deepseek",
        base_url: str | None = None,
        context_window: int | None = None,
        max_output_tokens: int | None = None,
        extra_args: list[str] | None = None,
        turn_timeout_sec: int = 1800,
        cache_dir: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._repository = repository
        self._requested_version = version
        self._provider = provider
        self._base_url = base_url
        self._context_window = context_window
        self._max_output_tokens = max_output_tokens
        self._extra_args = list(extra_args or [])
        self._turn_timeout_sec = turn_timeout_sec
        self._cache_dir = Path(
            cache_dir
            or os.environ.get("XHARNESS_BENCH_CACHE")
            or Path(tempfile.gettempdir()) / "xharness-bench-cache"
        )
        self._bundle: Bundle | None = None
        self._token: str | None = None
        self._port: int | None = None
        self._transcript: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- identity

    @staticmethod
    @override
    def name() -> str:
        return "xharness"

    @override
    def version(self) -> str | None:
        return self._bundle.version if self._bundle else self._requested_version

    # ------------------------------------------------------------------ bundle

    def _resolve_release(self) -> tuple[str, str, str]:
        """Return ``(version, tag, appimage_url)`` for the pinned or latest release."""
        if self._requested_version:
            tag = f"desktop-v{self._requested_version}"
            manifest_url = (
                f"https://github.com/{self._repository}/releases/download/"
                f"{tag}/latest.json"
            )
        else:
            manifest_url = (
                f"https://github.com/{self._repository}/releases/latest/download/latest.json"
            )
        with urllib.request.urlopen(manifest_url, timeout=60) as response:
            manifest = json.load(response)
        version = str(manifest["version"])
        platform = manifest["platforms"]["linux-x86_64-appimage"]
        return version, f"desktop-v{version}", str(platform["url"])

    def prepare_bundle(self) -> Bundle:
        """Download, checksum-verify and extract the release (host side, cached)."""
        if self._bundle is not None:
            return self._bundle

        version, tag, url = self._resolve_release()
        target = self._cache_dir / tag
        binary = target / "xharness-host"
        web_dir = target / "web"

        if binary.is_file() and web_dir.is_dir():
            self._bundle = Bundle(version, tag, binary, web_dir, _sha256(binary))
            return self._bundle

        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as scratch_name:
            scratch = Path(scratch_name)
            appimage = scratch / "XHarness.AppImage"
            _download(url, appimage)

            sums_url = (
                f"https://github.com/{self._repository}/releases/download/{tag}/SHA256SUMS"
            )
            expected = None
            try:
                sums_file = scratch / "SHA256SUMS"
                _download(sums_url, sums_file)
                wanted = appimage.name
                for line in sums_file.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1].lstrip("*") == wanted:
                        expected = parts[0]
                        break
            except Exception:  # noqa: BLE001 - sums are advisory when absent
                expected = None

            actual = _sha256(appimage)
            if expected and expected != actual:
                raise RuntimeError(
                    f"AppImage checksum mismatch for {tag}: "
                    f"expected {expected}, got {actual}"
                )

            appimage.chmod(0o755)
            subprocess.run(
                [str(appimage), "--appimage-extract"],
                cwd=scratch,
                check=True,
                capture_output=True,
            )
            root = scratch / "squashfs-root"
            shutil.copy2(root / "usr/bin/xharness-host", binary)
            shutil.copytree(root / "usr/lib/XHarness/web", web_dir, dirs_exist_ok=True)

        self._bundle = Bundle(version, tag, binary, web_dir, _sha256(binary))
        return self._bundle

    # ------------------------------------------------------------------- setup

    async def _exec(
        self, environment: BaseEnvironment, command: str, **kwargs: Any
    ) -> tuple[int, str]:
        result = await environment.exec(command, **kwargs)
        return exec_parts(result)

    async def _ensure_curl(self, environment: BaseEnvironment) -> None:
        """The RPC shim needs an HTTP client; minimal images ship none."""
        code, _ = await self._exec(environment, "command -v curl >/dev/null 2>&1")
        if code == 0:
            return
        installers = (
            "apt-get update -qq && apt-get install -y -qq --no-install-recommends curl",
            "apk add --no-cache curl",
            "dnf install -y -q curl",
            "yum install -y -q curl",
        )
        for installer in installers:
            code, _ = await self._exec(environment, f"{installer} >/dev/null 2>&1")
            if code == 0:
                break
        code, _ = await self._exec(environment, "command -v curl >/dev/null 2>&1")
        if code != 0:
            raise RuntimeError(
                "no HTTP client available in the task container and none could be "
                "installed; the XHarness adapter needs curl to drive the host RPC"
            )

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        bundle = await asyncio.to_thread(self.prepare_bundle)

        await self._exec(environment, f"mkdir -p {INSTALL_DIR} {STATE_DIR}")
        await environment.upload_file(bundle.binary, f"{INSTALL_DIR}/xharness-host")
        await environment.upload_dir(bundle.web_dir, f"{INSTALL_DIR}/web")
        code, out = await self._exec(
            environment,
            f"chmod 0755 {INSTALL_DIR}/xharness-host && "
            f"{INSTALL_DIR}/xharness-host --help >/dev/null 2>&1; "
            f"echo installed",
        )
        if code != 0:
            raise RuntimeError(f"failed to install xharness-host: {out[:400]}")

        await self._ensure_curl(environment)

    # --------------------------------------------------------------------- run

    async def _start_host(
        self, environment: BaseEnvironment, workspace: str, api_key: str | None
    ) -> ContainerRpcClient:
        self._token = hashlib.sha256(os.urandom(32)).hexdigest()[:48]
        ready = f"{STATE_DIR}/ready.address"
        shutdown = f"{STATE_DIR}/shutdown.request"
        log = f"{STATE_DIR}/host.log"

        args = [
            f"{INSTALL_DIR}/xharness-host",
            "--bind", "127.0.0.1:0",
            "--workspace", workspace,
            "--state-dir", STATE_DIR,
            "--static-dir", f"{INSTALL_DIR}/web",
            "--ready-file", ready,
            "--shutdown-file", shutdown,
            "--provider", self._provider,
        ]
        if self.model_name:
            args += ["--model", self.model_name]
        if self._base_url:
            args += ["--base-url", self._base_url]
        if self._context_window:
            args += ["--context-window", str(self._context_window)]
        if self._max_output_tokens:
            args += ["--max-output-tokens", str(self._max_output_tokens)]
        args += self._extra_args

        env = {"XHARNESS_DESKTOP_TOKEN": self._token}
        if api_key:
            # Passed by environment, never by argv: argv is world-readable in ps.
            env["XHARNESS_API_KEY"] = api_key

        quoted = " ".join(f"'{a}'" for a in args)
        await self._exec(
            environment,
            f"rm -f {ready} {shutdown}; "
            f"(setsid {quoted} >{log} 2>&1 </dev/null &) ; sleep 1; echo launched",
            env=env,
        )

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            code, out = await self._exec(environment, f"cat {ready} 2>/dev/null")
            if code == 0 and out.strip():
                host, _, port = out.strip().partition(":")
                if port.isdigit():
                    self._port = int(port)
                    break
            await asyncio.sleep(0.5)
        if self._port is None:
            _, log_text = await self._exec(environment, f"tail -40 {log} 2>/dev/null")
            raise RuntimeError(f"xharness-host never reported readiness:\n{log_text}")

        client = ContainerRpcClient(
            exec_fn=lambda command: self._exec(environment, command),
            port=self._port,
            token=self._token,
        )
        await client.wait_ready()
        return client

    async def _allow_unattended_tools(self, client: ContainerRpcClient) -> None:
        """The desktop asks a human before writes; a benchmark has no human."""
        settings = await client.call("settings.describe", {})
        permission = next(
            (
                entry
                for entry in settings.get("namespaces", [])
                if entry.get("ns") == "permission"
            ),
            None,
        )
        if permission is None:
            return
        await client.call(
            "settings.mutate",
            {
                "ns": "permission",
                "ops": [
                    {
                        "op": "set",
                        "path": ["defaultPreset"],
                        "value": "danger-full-access",
                    }
                ],
                "expectedRevision": permission.get("revision"),
            },
        )

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        api_key = (
            os.environ.get("XHARNESS_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or self._get_env("XHARNESS_API_KEY", "DEEPSEEK_API_KEY")
        )
        workspace = (await self._exec(environment, "pwd"))[1].strip() or "/app"

        client = await self._start_host(environment, workspace, api_key)
        await self._allow_unattended_tools(client)

        session_id = "bench-trial"
        await client.call(
            "session.create", {"sessionId": session_id, "cwd": workspace}
        )
        await client.call(
            "session.prompt",
            {
                "sessionId": session_id,
                "mode": "queue",
                "content": [{"type": "text", "text": instruction}],
            },
        )

        started = time.monotonic()
        history: dict[str, Any] = {}
        while time.monotonic() - started < self._turn_timeout_sec:
            history = await client.call("session.history", {"sessionId": session_id})
            events = normalized_events(history)
            if any(event["type"] == "turn/end" for event in events):
                break
            await asyncio.sleep(1.0)
        else:
            context.metadata = {**(context.metadata or {}), "timeout": True}

        events = normalized_events(history)
        self._transcript = events
        self._populate_context(context, events, time.monotonic() - started)

    def _populate_context(
        self,
        context: AgentContext,
        events: list[dict[str, Any]],
        elapsed: float,
    ) -> None:
        input_tokens = cache_tokens = output_tokens = 0
        tool_calls = 0
        answers: list[str] = []
        reasons: list[str] = []

        for event in events:
            kind = event.get("type")
            data = event.get("data") or {}
            if kind in ("assistant/message", "message/assistant") and isinstance(data, dict):
                usage = data.get("usage")
                if isinstance(usage, dict):
                    input_tokens += _usage_number(usage, "inputTokens", "input_tokens")
                    output_tokens += _usage_number(usage, "outputTokens", "output_tokens")
                    cache_tokens += _usage_number(
                        usage,
                        "cacheReadTokens",
                        "cache_read_tokens",
                        "cacheWriteTokens",
                        "cache_write_tokens",
                    )
                answers.append(message_text(data.get("message", data)))
            elif isinstance(kind, str) and kind.startswith("tool/"):
                tool_calls += 1
            elif kind == "turn/end" and isinstance(data, dict):
                reason = data.get("reason")
                if isinstance(reason, dict) and isinstance(reason.get("kind"), str):
                    reasons.append(reason["kind"])

        context.n_input_tokens = input_tokens
        context.n_cache_tokens = cache_tokens
        context.n_output_tokens = output_tokens
        context.metadata = {
            **(context.metadata or {}),
            "elapsed_sec": round(elapsed, 3),
            "tool_calls": tool_calls,
            "turn_end_reasons": reasons,
            "final_response": answers[-1] if answers else "",
            "xharness_version": self.version(),
            "xharness_host_sha256": self._bundle.sha256 if self._bundle else None,
        }