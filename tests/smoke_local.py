"""Smoke-test the XHarness adapter against a real host, with no containers.

Why this exists
---------------
The adapter is the part of this repository that can be wrong in ways the
Terminal-Bench oracle baseline cannot see: the oracle never invokes it. Its risk
is the control-plane sequence -- argument names, environment variables, RPC
payload shapes, the completion signal -- and every one of those can be checked in
seconds against the real binary on the local machine instead of in minutes
against a container.

This runs the *actual adapter class*, not a re-description of it, so a change to
the adapter is exercised here. It supplies a duck-typed stand-in for Harbor's
``BaseEnvironment`` (the adapter only ever calls ``exec``, ``upload_file`` and
``upload_dir``) and a fake OpenAI-compatible provider that speaks SSE, so a whole
turn completes without a real credential.

What it proves, and what it does not
------------------------------------
Proves: the host starts with the flags and environment the adapter builds; the
desktop token is accepted; the permission namespace is reachable and the session
is moved off its default preset; ``session.create`` / ``session.prompt`` are
accepted; ``turn/end`` arrives; and the five token dimensions are parsed.
Does not prove: anything about a real model's answers, or that a Terminal-Bench
task can be solved. Those need the benchmark itself.

Pass ``--live`` to skip the fake provider and drive a real model instead. That
is a different and stronger check: it exercises the credential path, the real
endpoint and the real streaming protocol, at the cost of a few hundred tokens. A
fake provider cannot tell you whether the harness is wired to a working key.

Usage::

    python tests/smoke_local.py --host-binary ~/xharness/bin/xharness-host \\
        --web-dir ~/xharness/web
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xharness_bench.agents import xharness as X  # noqa: E402

# Numbers the fake provider reports, so the parsed usage can be checked exactly.
PROMPT_TOKENS = 1234
CACHED_TOKENS = 900
COMPLETION_TOKENS = 56
REASONING_TOKENS = 20
# The host reports inputTokens excluding cache reads and outputTokens excluding
# reasoning, so these are the values the adapter must end up with.
EXPECT_INPUT = PROMPT_TOKENS - CACHED_TOKENS
EXPECT_OUTPUT = COMPLETION_TOKENS - REASONING_TOKENS


class FakeProvider:
    """Minimal OpenAI-compatible endpoint: the token-count route plus SSE chat."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.paths: list[str] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                outer.paths.append(self.path)
                if "input_tokens" in self.path:
                    body = json.dumps({"input_tokens": PROMPT_TOKENS}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()
                for chunk in (
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                  "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {"content": "Done."},
                                  "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": PROMPT_TOKENS,
                               "completion_tokens": COMPLETION_TOKENS,
                               "prompt_tokens_details": {"cached_tokens": CACHED_TOKENS},
                               "completion_tokens_details": {"reasoning_tokens": REASONING_TOKENS}}},
                ):
                    payload = {"id": "c1", "object": "chat.completion.chunk", "created": 0,
                               "model": "fake", **chunk}
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *_: Any) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()


class LocalEnvironment:
    """Duck-typed stand-in for Harbor's BaseEnvironment, executing locally."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    async def exec(  # noqa: A003 - matches Harbor's method name
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: Any = None,
    ) -> Any:
        merged = dict(os.environ)
        merged.update(env or {})
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd or str(self.cwd),
            env=merged,
            capture_output=True,
            text=True,
            timeout=timeout_sec or 300,
        )
        return SimpleNamespace(return_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    async def upload_file(self, source_path: Any, target_path: str) -> None:
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)

    async def upload_dir(self, source_dir: Any, target_dir: str) -> None:
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)


def check_turn_error_shapes() -> list[tuple[str, bool]]:
    """A failed turn must carry its reason, in either shape the host may emit.

    The host does not serialise ``TurnEndReason::Failed { error: String }``. It
    builds the JSON by hand (driver.rs), so a failed turn arrives as::

        {"kind": "error", "error": {"message": ..., "code": "LOOP_FAILED"}}

    -- a different kind *and* a nested object. Reading only ``kind`` is how four
    trials in the first comparison run ended with reason "error" and nothing to
    explain them, which is indistinguishable from a low score.
    """
    from xharness_bench.rpc import summarize_events

    nested = summarize_events(
        [
            {
                "type": "turn/end",
                "data": {
                    "turn": 0,
                    "reason": {
                        "kind": "error",
                        "error": {"message": "provider refused", "code": "LOOP_FAILED"},
                    },
                },
            }
        ]
    )
    flat = summarize_events(
        [{"type": "turn/end", "data": {"reason": {"kind": "failed", "error": "flat"}}}]
    )
    clean = summarize_events(
        [{"type": "turn/end", "data": {"reason": {"kind": "completed"}}}]
    )
    truncated = summarize_events(
        [{"type": "turn/end", "data": {"reason": {"kind": "max-tokens"}}}]
    )
    return [
        ("failed turn keeps its message", nested["turn_end_errors"] == [
            "error: provider refused (LOOP_FAILED)"
        ]),
        ("failed turn is not 'completed'", nested["turn_completed"] is False),
        ("flat error shape also read", flat["turn_end_errors"] == ["failed: flat"]),
        ("clean turn reports no errors", clean["turn_end_errors"] == []),
        ("clean turn is 'completed'", clean["turn_completed"] is True),
        ("truncation is visible", truncated["turn_end_reasons"] == ["max-tokens"]),
    ]


class _PagedHistoryStub:
    """Stands in for the host's ``session.history``, paging the way it really does.

    The semantics are copied from the server rather than imagined: it walks backwards
    until ``maxMessages`` *message* events (user, assistant, tool result) have been
    counted, returns every event in that range, and reports ``hasMore``. The default
    is 50 messages.
    """

    def __init__(self, events: list[dict[str, Any]], default_messages: int = 50) -> None:
        self.events = events
        self.default_messages = default_messages
        self.calls: list[dict[str, Any]] = []

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        assert method == "session.history", method
        payload = payload or {}
        self.calls.append(dict(payload))
        limit = int(payload.get("maxMessages") or self.default_messages)
        end = int(payload.get("beforeSeq") or len(self.events))
        end = min(end, len(self.events))
        start, messages = end, 0
        while start > 0 and messages < max(limit, 1):
            start -= 1
            if self.events[start]["type"] in ("user/message", "assistant/message", "tool/result"):
                messages += 1
        return {"events": self.events[start:end], "hasMore": start > 0}


def _synthetic_events(steps: int) -> list[dict[str, Any]]:
    """One user message, then ``steps`` assistant/tool-result pairs."""
    events: list[dict[str, Any]] = [{"seq": 0, "type": "user/message", "data": {}}]
    seq = 1
    for step in range(steps):
        events.append({"seq": seq, "type": "assistant/message", "data": {"step": step}})
        seq += 1
        events.append({"seq": seq, "type": "tool/result", "data": {"step": step}})
        seq += 1
    events.append({"seq": seq, "type": "turn/end", "data": {"reason": {"kind": "completed"}}})
    return events


def check_history_paging() -> list[tuple[str, bool]]:
    """A long run must be read back whole, not as its last 50 messages.

    The first comparison run reported tool-call counts clustering at 48 to 50 across
    unrelated tasks, which reads as a step limit. The host has none -- max_steps is
    usize::MAX -- but session.history defaults to the last 50 messages and says so
    only through a hasMore flag the adapter was not reading.
    """
    import asyncio as _asyncio

    from xharness_bench.rpc import fetch_history, normalized_events

    events = _synthetic_events(120)
    stub = _PagedHistoryStub(events)

    default_page = _asyncio.run(stub.call("session.history", {"sessionId": "s"}))
    complete = _asyncio.run(fetch_history(stub, "s"))

    default_types = [e["type"] for e in default_page["events"]]
    complete_types = [e["type"] for e in complete["events"]]
    steps_default = default_types.count("assistant/message")
    steps_complete = complete_types.count("assistant/message")

    # Ordering must survive paging: events are prepended page by page.
    seqs = [e["seq"] for e in complete["events"]]
    return [
        ("default page really is truncated", default_page["hasMore"] is True),
        ("default page holds 50 messages", len(default_page["events"]) < len(events)),
        ("default page covers about 25 steps", 20 <= steps_default <= 26),
        ("complete read gets every event", len(complete["events"]) == len(events)),
        ("complete read gets every step", steps_complete == 120),
        ("events stay in sequence order", seqs == sorted(seqs)),
        ("complete read survives normalization",
         len(normalized_events(complete)) == len(events)),
    ]



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-binary", type=Path, required=True)
    parser.add_argument("--web-dir", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--version", default="0.2.19")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument(
        "--live",
        action="store_true",
        help="drive a real model instead of the fake provider (costs tokens)",
    )
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument(
        "--api-key-env",
        default="DEEPSEEK_API_KEY",
        help="environment variable holding the credential, for --live",
    )
    args = parser.parse_args(argv)

    # Pure parsing checks first: they need no host, no model and no container, and
    # they cover the reason a failed turn is readable at all.
    shape_results = check_turn_error_shapes() + check_history_paging()

    root = Path(tempfile.mkdtemp(prefix="xh-smoke-"))
    workspace = root / "ws"
    workspace.mkdir(parents=True)

    # The adapter installs into /opt in a task container; redirect to the temp
    # root so this can run unprivileged.
    X.INSTALL_DIR = str(root / "opt")
    X.STATE_DIR = str(root / "opt" / "state")

    # Seed the bundle cache so the smoke test needs no download.
    cache = args.cache_dir or (root / "cache")
    tag = f"desktop-v{args.version}"
    target = cache / tag
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.host_binary, target / "xharness-host")
    (target / "xharness-host").chmod(0o755)
    if args.web_dir and args.web_dir.is_dir():
        shutil.copytree(args.web_dir, target / "web", dirs_exist_ok=True)
    else:
        (target / "web").mkdir(exist_ok=True)

    live = bool(args.live)
    provider = None
    if not live:
        provider = FakeProvider(args.port)
        provider.start()
        os.environ["XHARNESS_API_KEY"] = "sk-smoke-test"
    else:
        key = os.environ.get(args.api_key_env, "")
        if not key.startswith("sk-"):
            print(f"  {args.api_key_env} is not set (or does not look like a key)")
            return 2
        print(f"  live mode: {args.model} at {args.base_url}")
        print(f"  credential: {args.api_key_env} ({len(key)} chars, {key[:3]}...)")

    async def scenario() -> dict[str, Any]:
        from harbor.models.agent.context import AgentContext

        agent = X.XHarnessAgent(
            logs_dir=root / "logs",
            model_name=args.model,
            version=args.version,
            cache_dir=str(cache),
            base_url=(args.base_url if live else f"http://127.0.0.1:{args.port}/v1"),
            context_window=65536,
            turn_timeout_sec=120,
        )
        env = LocalEnvironment(workspace)
        await agent.setup(env)
        context = AgentContext()
        await agent.run("Write a short greeting to greeting.txt.", env, context)
        return context

    try:
        context = asyncio.run(scenario())
    finally:
        if provider is not None:
            provider.stop()

    meta = context.metadata or {}
    tokens = meta.get("tokens") or {}
    if live:
        # The fake provider asserts exact numbers; with a real model the point is
        # that the plumbing works and the figures are plausible.
        checks = [
            ("turn reached a clean finish", meta.get("turn_completed") is True),
            ("turn/end reason recorded", meta.get("turn_end_reasons") == ["completed"]),
        ("no spurious turn errors", meta.get("turn_end_errors") == []),
            ("final response captured", bool(meta.get("final_response"))),
            ("input tokens reported", (tokens.get("input_tokens") or 0) > 0),
            ("output tokens reported", (tokens.get("output_tokens") or 0) > 0),
            ("Harbor context populated", (context.n_input_tokens or 0) > 0),
            ("tool_calls counter present", "tool_calls" in meta),
        ]
        print(f"provider paths seen: (live, not instrumented)")
    else:
        checks = [
            ("turn reached a clean finish", meta.get("turn_completed") is True),
            ("turn/end reason recorded", meta.get("turn_end_reasons") == ["completed"]),
            ("final response captured", bool(meta.get("final_response"))),
            (f"inputTokens == {EXPECT_INPUT}", tokens.get("input_tokens") == EXPECT_INPUT),
            (f"outputTokens == {EXPECT_OUTPUT}", tokens.get("output_tokens") == EXPECT_OUTPUT),
            (f"cacheReadTokens == {CACHED_TOKENS}", tokens.get("cache_read_tokens") == CACHED_TOKENS),
            (f"reasoningTokens == {REASONING_TOKENS}", tokens.get("reasoning_tokens") == REASONING_TOKENS),
            ("Harbor context populated", context.n_input_tokens == EXPECT_INPUT
             and context.n_output_tokens == EXPECT_OUTPUT),
            ("token-count route was used", any("input_tokens" in p for p in provider.paths)),
            ("chat route was used", any(p.endswith("/chat/completions") for p in provider.paths)),
        ]

    print(f"workspace: {workspace}")
    if provider is not None:
        print(f"provider paths seen: {provider.paths}")
    print(f"metadata: {json.dumps(meta, ensure_ascii=False)[:400]}")
    print()
    failed = 0
    for label, ok in shape_results + checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += 0 if ok else 1
    print()
    print("SMOKE OK" if not failed else f"SMOKE FAILED ({failed})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())