"""JSON-RPC client for the XHarness host control plane.

The host exposes one HTTP endpoint per RPC method::

    POST /api/<method>
    {"type": "client-request", "rpcId": "...", "method": "...", "payload": {...}}

and answers ``{"result": {"ok": true, "value": ...}}``.  A short envelope
protocol rather than bare JSON-RPC 2.0 -- see ``crates/xharness-api/src/lib.rs``
(``rpc_methods!``) for the authoritative method list.

This module does not speak HTTP itself.  It is handed a coroutine that runs a
shell command *inside the task container*, because that is the only transport
every Harbor environment backend supports uniformly (Docker, Daytona, Modal,
...).  Talking to the container's own loopback keeps the adapter portable: no
port publishing, no bridge-IP assumptions, no dependence on the host network
namespace.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Awaitable, Callable

# Runs a shell command in the task container and returns (exit_code, stdout).
ExecFn = Callable[[str], Awaitable[tuple[int, str]]]


class RpcError(RuntimeError):
    """The host answered, but the envelope said the call failed."""


class TransportError(RuntimeError):
    """No usable answer came back (host down, curl missing, timeout)."""


def exec_parts(result: Any) -> tuple[int, str]:
    """Normalise a Harbor ``ExecResult`` to ``(exit_code, stdout)``.

    Harbor has used both ``return_code`` and ``exit_code`` across versions; the
    adapter should not break because of that rename.
    """
    code = getattr(result, "return_code", None)
    if code is None:
        code = getattr(result, "exit_code", 0)
    stdout = getattr(result, "stdout", "") or ""
    return int(code), stdout


class ContainerRpcClient:
    """Drive an XHarness host listening on the container's loopback."""

    def __init__(
        self,
        exec_fn: ExecFn,
        port: int,
        token: str | None = None,
        label: str = "bench",
    ) -> None:
        self._exec = exec_fn
        self._port = port
        self._token = token
        self._label = label
        self._sequence = 0

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> Any:
        self._sequence += 1
        envelope = json.dumps(
            {
                "type": "client-request",
                "rpcId": f"{self._label}-{self._sequence}",
                "method": method,
                "payload": payload or {},
            },
            ensure_ascii=False,
        )
        parts = [
            "curl",
            "-sS",
            "--max-time",
            str(int(timeout)),
            "-H",
            shlex.quote("content-type: application/json"),
        ]
        if self._token:
            parts += [
                "-H",
                shlex.quote(f"x-xharness-desktop-token: {self._token}"),
            ]
        parts += [
            "--data-binary",
            shlex.quote(envelope),
            shlex.quote(f"{self.origin}/api/{method}"),
        ]
        code, out = await self._exec(" ".join(parts))
        if code != 0 or not out.strip():
            raise TransportError(
                f"RPC {method} produced no response (exit {code}): {out[:400]!r}"
            )
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError as error:
            raise TransportError(
                f"RPC {method} returned non-JSON: {out[:400]!r}"
            ) from error
        result = parsed.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RpcError(
                f"RPC {method} failed: {json.dumps(parsed, ensure_ascii=False)[:600]}"
            )
        return result.get("value")

    async def wait_ready(self, timeout: float = 60.0, interval: float = 0.25) -> None:
        """Poll a cheap read-only method until the host answers.

        ``workspace.list`` is used because it touches no session state.
        """
        import asyncio
        import time

        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                await self.call("workspace.list", {}, timeout=20.0)
                return
            except (TransportError, RpcError) as error:
                last = error
                await asyncio.sleep(interval)
        raise TransportError(f"host never became ready within {timeout:.0f}s: {last}")


def normalized_events(history: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``session.history`` into a list of typed session events."""
    events: list[dict[str, Any]] = []
    for entry in history.get("events", []) or []:
        event = entry.get("event", entry) if isinstance(entry, dict) else None
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            events.append(event)
    return events


def message_text(message: dict[str, Any]) -> str:
    """Concatenate the text parts of one assistant message."""
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    return "".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )