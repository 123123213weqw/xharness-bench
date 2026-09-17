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

from ..rpc import exec_parts, summarize_events

FROZEN_CONTRACT_REVISION = "deepseek-harness@141eb6fef8"
FROZEN_UPSTREAM_TAG = "dsh-v0.1.0-rc.8"

# The tag above is what the replica froze against, and it exists on GitHub. It
# does NOT exist on PyPI: the 0.1.0 series there stops at ``0.1.0rc7``. So the
# closest installable upstream is one release candidate below the frozen
# revision, and that drift has to be stated rather than papered over -- it is the
# difference between measuring replica fidelity and measuring upstream progress.
#
# Every PyPI release of this SDK is a pre-release (rc/a/dev), so ``pip install``
# needs ``--pre``; without it pip reports "No matching distribution found", which
# reads like a typo rather than a flag omission.
NEAREST_INSTALLABLE_TO_FROZEN = "0.1.0rc7"

# The endpoint the pinned provider talks to.
DEFAULT_BASE_URL = "https://api.deepseek.com"

DRIVER = textwrap.dedent(
    '''
    """One-shot upstream Harness turn; prints a single JSON result object."""
    import inspect, json, os, sys, time

    from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig

    payload = json.loads(os.environ["BENCH_TURN"])
    started = time.monotonic()

    # The SDK's configuration surface was reworked between the 0.1.0 series and
    # 0.1.5. rc7 takes `session_root` and has no notion of a harness home; 0.1.5
    # takes `dsh_home` and adds profiles and patches. Rather than pinning the
    # adapter to one shape, build the kwargs from the installed Config's own
    # signature -- so the arm can name whichever version is closest to the frozen
    # contract revision without the adapter breaking on the difference.
    fields = set(inspect.signature(DeepSeekHarnessConfig.__init__).parameters)
    kwargs = {
        "provider": payload["provider"],
        "model": payload["model"],
        "cwd": payload["cwd"],
    }
    if "dsh_home" in fields:
        kwargs["dsh_home"] = payload["dsh_home"]
    elif "session_root" in fields:
        kwargs["session_root"] = payload["dsh_home"]
    if payload.get("max_tokens"):
        kwargs["max_tokens"] = payload["max_tokens"]
    if payload.get("base_url"):
        kwargs["base_url"] = payload["base_url"]
    if payload.get("api_key"):
        kwargs["api_key"] = payload["api_key"]
    if "profile" in fields and payload.get("profile"):
        kwargs["profile"] = payload["profile"]

    shape = "dsh_home" if "dsh_home" in fields else (
        "session_root" if "session_root" in fields else "unknown"
    )

    result = {"ok": False}
    try:
        with DeepSeekHarness(**kwargs) as harness:
            run = harness.run(payload["instruction"], session_id="bench-trial")
        result = {
            "ok": True,
            "config_shape": shape,
            "config_fields": sorted(fields),
            "final_response": run.final_response,
            "finish_reason": getattr(run, "finish_reason", None),
            "events": [e for e in (getattr(run, "events", None) or []) if isinstance(e, dict)],
        }
    except Exception as error:
        result = {
            "ok": False,
            "config_shape": shape,
            "config_fields": sorted(fields),
            "error": f"{type(error).__name__}: {error}",
        }
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
        base_url: str | None = DEFAULT_BASE_URL,
        profile: str | None = None,
        dsh_home: str = "/opt/dsh-home",
        turn_timeout_sec: int = 1800,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._sdk_version = sdk_version
        self._provider = provider
        self._max_tokens = max_tokens
        self._base_url = base_url
        self._profile = profile
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
        version = self._sdk_version or NEAREST_INSTALLABLE_TO_FROZEN
        spec = f"{spec}=={version}"
        # Provision through uv, not `python3 -m venv` plus pip.
        #
        # The previous version guarded the install with
        #     command -v python3 || apt-get install ... python3-venv
        # which is wrong twice over: an image that has python3 without the venv
        # module takes the first branch and then fails at `python3 -m venv`, and an
        # image with neither python3 nor apt fails in the fallback. Most task images
        # are that second case -- no python3, no curl, no apt -- so 42 of 47 trials
        # died with
        #     bash: /opt/dsh-venv/bin/pip: No such file or directory
        # Those images do carry uv, however, and a managed CPython is bind-mounted
        # beside the package cache. uv needs neither ensurepip nor a package
        # manager, so provisioning stops depending on what the image happens to
        # contain.
        # --offline first, and that is not an optimisation.
        #
        # The warmed cache is a bind mount and does contain the wheels, but uv still
        # consults the index when the cached index response has aged out, so a run
        # that should be entirely local goes to pypi.org and gives up:
        #
        #     error: Failed to fetch: `https://pypi.org/simple/deepseek-harness-sdk/`
        #       Caused by: Request failed after 3 retries
        #       Caused by: operation timed out
        #
        # That is intermittent -- it depends on how long ago the cache was written --
        # which is worse than a consistent failure, because the arm it kills looks
        # like a property of the harness. Seven of 47 trials in the second comparison
        # run died this way, and whether they did was a function of cache age. With
        # --offline the resolution is deterministic: it either finds the wheels or it
        # does not, and it does not silently depend on the network being up.
        provision = (
            "uv venv --python 3.12 /opt/dsh-venv 2>&1 | tail -3; "
            f"uv pip install --offline --python /opt/dsh-venv/bin/python "
            f"{shlex.quote(spec)} 2>&1 | tail -8 || "
            f"uv pip install --python /opt/dsh-venv/bin/python {shlex.quote(spec)}"
            " 2>&1 | tail -8; "
            "/opt/dsh-venv/bin/python -c 'import deepseek_harness; "
            "print(deepseek_harness.__file__)'"
        )
        code, out = await self._exec(environment, provision, timeout_sec=1800)
        if code != 0:
            # Second attempt, for an image that predates the uv bake: plain pip, if
            # the image can manage it at all.
            legacy = (
                "python3 -m venv /opt/dsh-venv >/dev/null 2>&1; "
                f"/opt/dsh-venv/bin/pip install --quiet --pre {shlex.quote(spec)}"
                " 2>&1 | tail -5; "
                "/opt/dsh-venv/bin/python -c 'import deepseek_harness; "
                "print(deepseek_harness.__file__)'"
            )
            code, legacy_out = await self._exec(environment, legacy, timeout_sec=1800)
            if code != 0:
                raise RuntimeError(
                    f"failed to install the upstream SDK ({spec}). "
                    f"The uv route said:\n{out[:500]}"
                    f"\nand the plain-pip route said:\n{legacy_out[:500]}"
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
            "base_url": self._base_url,
            "profile": self._profile,
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

        # Same parser as the XHarness adapter: the two emit identical events, so
        # sharing the implementation keeps a parsing difference from masquerading
        # as a harness difference.
        summary = summarize_events(result.get("events") or [])

        context.n_input_tokens = summary["input_tokens"]
        context.n_cache_tokens = summary["cache_read_tokens"] + summary["cache_write_tokens"]
        context.n_output_tokens = summary["output_tokens"]
        context.metadata = {
            **(context.metadata or {}),
            "elapsed_sec": result.get("elapsed_sec"),
            "final_response": result.get("final_response") or summary["final_response"],
            "turn_ok": result.get("ok"),
            "turn_error": result.get("error"),
            "finish_reason": result.get("finish_reason"),
            "config_shape": result.get("config_shape"),
            "turn_end_reasons": summary["turn_end_reasons"],
            "turn_end_errors": summary.get("turn_end_errors") or [],
            "effective_context_window": summary.get("effective_context_window"),
            "turn_completed": summary["turn_completed"],
            "tool_calls": summary["tool_calls"],
            # Recorded so a result row always says which upstream it measured.
            "contract_revision": FROZEN_CONTRACT_REVISION,
            "frozen_tag": FROZEN_UPSTREAM_TAG,
            "sdk_version": self._sdk_version or NEAREST_INSTALLABLE_TO_FROZEN,
            "sdk_version_is_frozen_tag": False,
            "exit_code": code,
            "tokens": {
                "input_tokens": summary["input_tokens"],
                "output_tokens": summary["output_tokens"],
                "cache_read_tokens": summary["cache_read_tokens"],
                "cache_write_tokens": summary["cache_write_tokens"],
                "reasoning_tokens": summary["reasoning_tokens"],
                "total_tokens_reported": summary["total_tokens_reported"],
            },
        }
