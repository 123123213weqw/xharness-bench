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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-binary", type=Path, required=True)
    parser.add_argument("--web-dir", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--version", default="0.2.19")
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args(argv)

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

    provider = FakeProvider(args.port)
    provider.start()
    os.environ["XHARNESS_API_KEY"] = "sk-smoke-test"

    async def scenario() -> dict[str, Any]:
        from harbor.models.agent.context import AgentContext

        agent = X.XHarnessAgent(
            logs_dir=root / "logs",
            model_name="deepseek-v4-flash",
            version=args.version,
            cache_dir=str(cache),
            base_url=f"http://127.0.0.1:{args.port}/v1",
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
        provider.stop()

    meta = context.metadata or {}
    tokens = meta.get("tokens") or {}
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
    print(f"provider paths seen: {provider.paths}")
    print(f"metadata: {json.dumps(meta, ensure_ascii=False)[:400]}")
    print()
    failed = 0
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += 0 if ok else 1
    print()
    print("SMOKE OK" if not failed else f"SMOKE FAILED ({failed})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())