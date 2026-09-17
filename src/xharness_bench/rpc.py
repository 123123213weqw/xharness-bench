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
import os
import shlex
from typing import Any, Awaitable, Callable

# Runs a shell command in the task container and returns (exit_code, stdout).
ExecFn = Callable[[str], Awaitable[tuple[int, str]]]


class RpcError(RuntimeError):
    """The host answered, but the envelope said the call failed."""


class TransportError(RuntimeError):
    """No usable answer came back (host down, curl missing, timeout)."""


# Installed into the task container at setup time. Kept as stdlib-only Python so
# the adapter does not need curl, apt, pip or any network: an interpreter is the
# one thing every task image can be made to have, because the baked images carry
# uv and a managed CPython is bind-mounted alongside the package cache.
RPC_SHIM = '''import json, sys, urllib.request, urllib.error


def main():
    origin, method, token, timeout = (
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
        float(sys.argv[4]),
    )
    body = sys.stdin.buffer.read()
    headers = {"content-type": "application/json"}
    if token:
        headers["x-xharness-desktop-token"] = token
    request = urllib.request.Request(
        f"{origin}/api/{method}", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            sys.stdout.buffer.write(response.read())
    except urllib.error.HTTPError as error:
        # An HTTP error still carries the JSON envelope, so let the caller read it
        # and decide. Exiting non-zero here would hide the host's own message.
        sys.stdout.buffer.write(error.read())
    except Exception as error:
        print(f"transport: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(3)


main()
'''


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
        python: str | None = None,
        script: str | None = None,
    ) -> None:
        self._exec = exec_fn
        self._port = port
        self._token = token
        self._label = label
        self._sequence = 0
        # When both are set the transport runs RPC_SHIM under that interpreter.
        # Otherwise it falls back to curl, which is what the adapter used first and
        # what works on images that happen to ship curl.
        self._python = python
        self._script = script

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
        if self._python and self._script:
            # Heredoc rather than argv: request/context payloads carry the whole
            # conversation and can exceed the argument list limit, and a heredoc
            # needs no escaping of the JSON at all. The delimiter is nonced so a
            # payload line can never terminate it early.
            nonce = f"XHARNESS_RPC_{self._sequence}_{os.urandom(4).hex()}"
            command = (
                f"{shlex.quote(self._python)} {shlex.quote(self._script)} "
                f"{shlex.quote(self.origin)} {shlex.quote(method)} "
                f"{shlex.quote(self._token or '')} {int(timeout)}"
                f" <<'{nonce}'\n{envelope}\n{nonce}"
            )
        else:
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
            command = " ".join(parts)
        code, out = await self._exec(command)
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


# --------------------------------------------------------------- event summary
#
# Both arms of the Tier A comparison emit the *same* session events. That is the
# point of the replica, and it is measured rather than assumed: driving upstream
# `dsh` through its Python SDK and XHarness through its loopback RPC produced the
# same event types for the same turn --
#
#     turn/start, step/start, system/message, user/message, request/header,
#     request/context, session/title, assistant/message, step/end, turn/end
#
# -- and the same nesting, ``{"type": ..., "seq": ..., "time": ..., "data": {...}}``.
# So the parsing below is shared deliberately: one implementation means a bug
# cannot make one arm look better than the other, which is exactly the failure
# mode a comparison like this has to defend against.
#
# The token dimensions were cross-checked on both sides and agree:
#
#   XHarness   inputTokens=334  outputTokens=36  cacheReadTokens=900  reasoningTokens=20
#              (fake provider reporting prompt=1234, cached=900, completion=56, reasoning=20)
#   upstream   inputTokens=6367 outputTokens=2   cacheReadTokens=768  reasoningTokens=0
#
# and upstream's ``totalTokens=7137`` equals ``6367 + 768 + 2`` exactly. Both
# agree that inputTokens EXCLUDES cache reads, so summing the dimensions is safe
# and dropping reasoning understates cost.


def _usage_number(usage: dict[str, Any], *names: str) -> int:
    """Read a token count that may be camelCase or snake_case."""
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a session's events to the numbers a result row needs.

    Returns the five token dimensions, the turn-end reasons, a tool-call count and
    the last assistant text. A missing dimension is reported as 0 rather than
    omitted, so an arm that never reports reasoning is visibly different from one
    that reports zero.
    """
    input_tokens = output_tokens = 0
    cache_read = cache_write = reasoning = 0
    total_reported = 0
    tool_calls = 0
    # What the harness actually ran with, read back from its own events rather than
    # assumed from the flags we passed. The two implementations do not mean the same
    # thing by "context window": on this model the upstream shipped config declares
    # 128e3 while the XHarness model profile declares 1_000_000, and either may
    # narrow the value it is given. Recording the effective number is the only way a
    # reader can tell whether the arms were granted comparable room.
    effective_context_window: int | None = None
    answers: list[str] = []
    reasons: list[str] = []
    errors: list[str] = []

    for event in events:
        kind = event.get("type")
        data = event.get("data") or {}
        if not isinstance(data, dict):
            continue

        if kind == "request/context":
            window = data.get("contextWindow") or data.get("context_window")
            if isinstance(window, int) and window > 0:
                effective_context_window = window

        if kind in ("assistant/message", "message/assistant"):
            usage = data.get("usage")
            if isinstance(usage, dict):
                input_tokens += _usage_number(usage, "inputTokens", "input_tokens")
                output_tokens += _usage_number(usage, "outputTokens", "output_tokens")
                cache_read += _usage_number(usage, "cacheReadTokens", "cache_read_tokens")
                cache_write += _usage_number(usage, "cacheWriteTokens", "cache_write_tokens")
                reasoning += _usage_number(usage, "reasoningTokens", "reasoning_tokens")
                total_reported += _usage_number(usage, "totalTokens", "total_tokens")
            message = data.get("message", data)
            if isinstance(message, dict):
                answers.append(message_text(message))

        elif isinstance(kind, str) and kind.startswith("tool/"):
            tool_calls += 1

        elif kind == "turn/end":
            reason = data.get("reason")
            if isinstance(reason, dict) and isinstance(reason.get("kind"), str):
                reasons.append(reason["kind"])
                # Failed carries the provider's own message, and the shape is not
                # the one the Rust enum suggests. The host does not serialise
                # TurnEndReason::Failed { error: String }; it builds the JSON by
                # hand in driver.rs:
                #     LoopStatus::Failed => json!({
                #         "kind": "error",
                #         "error": {"message": ..., "code": "LOOP_FAILED"},
                #     }),
                # so the kind is "error" rather than "failed", and the payload is a
                # nested object rather than a string. Reading only the kind made a
                # failing turn indistinguishable from a low score -- which is how
                # four trials in the first comparison run ended with reason "error"
                # and nothing whatsoever to explain them.
                detail = reason.get("error")
                if isinstance(detail, dict):
                    message = str(detail.get("message") or "").strip()
                    code = str(detail.get("code") or "").strip()
                    if message or code:
                        errors.append(
                            f"{reason['kind']}: {message}"
                            + (f" ({code})" if code else "")
                        )
                elif isinstance(detail, str) and detail.strip():
                    # Defensive: also accept the flat shape.
                    errors.append(f"{reason['kind']}: {detail.strip()}")

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
        # Not every implementation reports a total; recorded, not relied upon.
        "total_tokens_reported": total_reported,
        "tool_calls": tool_calls,
        "turn_end_reasons": reasons,
        # The provider's own message for any turn that ended in "failed".
        "turn_end_errors": errors,
        "effective_context_window": effective_context_window,
        # "completed" is the clean finish. Anything else means the harness itself
        # failed, which is not evidence about the model or the task.
        "turn_completed": "completed" in reasons,
        "final_response": answers[-1] if answers else "",
    }
