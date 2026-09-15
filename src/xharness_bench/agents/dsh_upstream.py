"""Harbor agent adapter for the upstream DeepSeek Harness (the thing XHarness replicates).

Why this is the interesting control
-----------------------------------
``deepseek-harness`` is the TypeScript/Node reference implementation.
``x-harness-rs`` is a Rust re-implementation of it, and it tracks that
faithfully on purpose: ``docs/compat/MATRIX.md`` reports 52/52 fixed RPC names,
10/10 mux frames, 10/10 host frames.  Driving the same task, prompt, tool
surface and model through both therefore isolates *implementation* as the only
free variable.  That is a far cleaner experiment than pitting two products with
different system prompts against each other.

Two ways to drive upstream, and why this file takes the second
--------------------------------------------------------------
1. Upstream ships an ACP surface (``packages/acp``) and Harbor ships an ``acp``
   agent (``harbor.agents.installed.acp:AcpAgent``).  If that path works it is
   zero adapter code -- prefer it, and prefer reporting it if it does.
   XHarness does **not** implement ACP (verified: no matches for ``acp`` under
   ``crates/`` or ``apps/``), so this route exists for one side only and cannot
   be used for the paired comparison.
2. The Python SDK (``pip install deepseek-harness-sdk``) bundles the native
   runtime, needs no system Node.js, and is drivable from a small script.  This
   file takes that route because it works symmetrically enough to pair against
   the XHarness adapter, and because it lets us install and pin a version inside
   the task container.

Version pinning is mandatory here.  The replica's frozen contract revision is
``deepseek-harness@141eb6fef8`` which corresponds to upstream tag
``dsh-v0.1.0-rc.8``.  Comparing the replica against a different upstream version
would measure upstream drift, not replica fidelity, so ``sdk_version`` must be
set explicitly and recorded in every result row.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import textwrap
from typing import Any, override

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ..rpc import exec_parts

FROZEN_UPSTREAM_TAG = "dsh-v0.1.0-rc.8"
FROZEN_CONTRACT_REVISION = "deepseek-harness@141eb6fef8"

DRIVER = textwrap.dedent(
    '''
    """One-shot upstream Harness turn; prints a single JSON result object."""
    import json, os, sys, time
    from deepseek_harness import DeepSeekHarness

    payload = json.loads(os.environ["BENCH_TURN"])
    started = time.monotonic()
    result = {"ok": False}
    try:
        with DeepSeekHarness(
            dsh_home=payload["dsh_home"],
            cwd=payload["cwd"],
            provider=payload["provider"],
            model=payload["model"],
            **({"max_tokens": payload["max_tokens"]} if payload.get("max_tokens") else {}),
        ) as harness:
            run = harness.run(payload["instruction"], session_id="bench-trial")
        result = {
            "ok": True,
            "final_response": run.final_response,
            "usage": getattr(run, "usage", None),
            "raw": {k: str(v) for k, v in vars(run).items()} if hasattr(run, "__dict__") else None,
        }
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    result["elapsed_sec"] = round(time.monotonic() - started, 3)
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)
    '''
).strip()


class DshUpstreamAgent(BaseAgent):
    """Drive the upstream DeepSeek Harness inside a task container."""

    capabilities = AgentCapabilities(windows=False)

    def __init__(
        self,
        *args: Any,
        sdk_version: str | None = None,
        provider: str = "deepseek-official",
        max_tokens: int | None = None,
        dsh_home: str = "/opt/dsh-home",
        turn_timeout_sec: int = 1800,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._sdk_version = sdk_version
        self._provider = provider
        self._max_tokens = max_tokens
        self._dsh_home = dsh_home
        self._turn_timeout_sec = turn_timeout_sec

    @staticmethod
    @override
    def name() -> str:
        return "dsh-upstream"

    @override
    def version(self) -> str | None:
        return self._sdk_version or FROZEN_UPSTREAM_TAG

    async def _exec(
        self, environment: BaseEnvironment, command: str, **kwargs: Any
    ) -> tuple[int, str]:
        return exec_parts(await environment.exec(command, **kwargs))

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        spec = "deepseek-harness-sdk"
        if self._sdk_version:
            spec = f"{spec}=={self._sdk_version}"
        code, out = await self._exec(
            environment,
            "command -v python3 >/dev/null 2>&1 || "
            "(apt-get update -qq && apt-get install -y -qq --no-install-recommends "
            "python3 python3-venv python3-pip) >/dev/null 2>&1; "
            "python3 -m venv /opt/dsh-venv >/dev/null 2>&1; "
            f"/opt/dsh-venv/bin/pip install --quiet --upgrade pip >/dev/null 2>&1; "
            f"/opt/dsh-venv/bin/pip install --quiet {shlex.quote(spec)} 2>&1 | tail -5; "
            "/opt/dsh-venv/bin/python -c 'import deepseek_harness, sys; "
            "print(deepseek_harness.__file__)'",
            timeout_sec=1800,
        )
        if code != 0:
            raise RuntimeError(
                f"failed to install the upstream SDK ({spec}); if the container has "
                f"no network, ship a wheel alongside the task image instead:\n{out[:600]}"
            )
        await environment.exec(
            f"mkdir -p {shlex.quote(self._dsh_home)} /opt/dsh-venv/driver"
        )
        driver = f"/opt/dsh-venv/driver/turn.py"
        await self._write_driver(environment, driver)

    async def _write_driver(self, environment: BaseEnvironment, path: str) -> None:
        """Install the one-shot driver without quoting hazards."""
        import base64

        encoded = base64.b64encode(DRIVER.encode()).decode()
        code, out = await self._exec(
            environment,
            f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)} && "
            f"test -s {shlex.quote(path)} && echo ok",
        )
        if code != 0:
            raise RuntimeError(f"could not install the turn driver: {out[:300]}")

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        workspace = (await self._exec(environment, "pwd"))[1].strip() or "/app"
        payload = {
            "instruction": instruction,
            "dsh_home": self._dsh_home,
            "cwd": workspace,
            "provider": self._provider,
            "model": self.model_name or "deepseek-v4-flash",
            "max_tokens": self._max_tokens,
        }
        api_key = (
            os.environ.get("DEEPSEEK_API_KEY")
            or self._get_env("DEEPSEEK_API_KEY", "XHARNESS_API_KEY")
        )
        env = {"BENCH_TURN": json.dumps(payload)}
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
        if os.environ.get("DEEPSEEK_BASE_URL"):
            env["DEEPSEEK_BASE_URL"] = os.environ["DEEPSEEK_BASE_URL"]

        code, out = await self._exec(
            environment,
            f"/opt/dsh-venv/bin/python /opt/dsh-venv/driver/turn.py",
            env=env,
            timeout_sec=self._turn_timeout_sec,
        )

        result: dict[str, Any] = {}
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    result = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        usage = result.get("usage") or {}
        if isinstance(usage, dict):
            context.n_input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            context.n_output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            context.n_cache_tokens = int(
                usage.get("cache_read_tokens") or usage.get("cached_tokens") or 0
            )
        context.metadata = {
            **(context.metadata or {}),
            "elapsed_sec": result.get("elapsed_sec"),
            "final_response": result.get("final_response", ""),
            "turn_ok": result.get("ok"),
            "turn_error": result.get("error"),
            "upstream_tag": FROZEN_UPSTREAM_TAG,
            "contract_revision": FROZEN_CONTRACT_REVISION,
            "sdk_version": self._sdk_version,
            "exit_code": code,
        }