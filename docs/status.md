# Status

What is verified, what is assumed, and what remains. Kept current rather than
optimistic.

## Verified

| Claim | How |
| --- | --- |
| `xharness-host` runs with only libc/libm/libgcc | `ldd` on the extracted binary |
| The host drives a full turn over JSON-RPC | it ran the in-repo reference eval; `turn/end` observed |
| Release artifacts are checksummed | `SHA256SUMS` fetched and matched on a real download |
| The AppImage extracts to a usable `xharness-host` + web assets | done repeatedly on the target host |
| Harbor 0.23.0 provides oracle / nop / mini-swe-agent / commercial agents | read from the installed package |
| Custom agents load by import path | `AgentFactory.create_agent_from_import_path` |
| `BaseEnvironment` offers `exec` / `upload_file` / `upload_dir` | read from the installed package |
| Docker works on the target host behind a registry mirror | pulled and ran `hello-world`, `ubuntu:24.04`, `python:3.11-slim` |
| Upstream is public and the frozen revision is a tag | `git ls-remote` |
| Both adapters load through Harbor's own factory | `AgentFactory.create_agent_from_import_path(...)` returned instances under Harbor 0.23.0 |
| The RPC envelope format is correct | unit test asserts the `client-request` envelope, the `x-xharness-desktop-token` header and `RpcError` on a failed envelope |
| `report.py` statistics are correct | checked against published values: Wilson(0,10)=[0, 0.2775], Wilson(5,10)=[0.2366, 0.7634], exact McNemar(10,2)=0.03857, symmetry, and n≈194 / n≈783 for 10pp / 5pp |
| The report CLI parses and classifies | run against synthetic job JSON in both supported shapes; produced per-harness rates with CIs, the failure taxonomy, a paired McNemar comparison and the resolution-floor warning |
| Task-set freezing detects a swapped task | freeze 5 IDs, re-verify the same list (OK) and a 4-ID list (MISMATCH, exit 1); digest confirmed order-independent |

## Assumed, not yet verified

| Assumption | Risk if wrong |
| --- | --- |
| The adapter's RPC sequence matches the live host | the run fails at the first call; the sequence is copied from the in-repo driver so this is likely but untested *through Harbor* |
| `ExecResult` exposes `return_code`/`stdout` | normalised in `rpc.exec_parts` to tolerate either naming, but untested |
| Task containers permit installing `curl` | `setup()` tries apt/apk/dnf/yum and fails loudly; minimal images without a package manager would need a bundled client |
| The upstream SDK version matching `dsh-v0.1.0-rc.8` exists on PyPI | the Tier A upstream arm cannot be installed; resolve before running |
| Terminal-Bench task images reach a package mirror | `setup()` would fail; a task set with vendored dependencies avoids this |

## Four tasks cannot pass the gate, and they are the tasks' fault

Measured against a set published 2025-10-31: `build-pov-ray` (its source URL
returns 403 to direct *and* proxied requests alike), `make-doom-for-mips` and
`build-pmars` (pinned Debian package versions that the archive has since removed
-- verified by fetching the exact `.deb` and getting 404), and `build-cython-ext`
(a test failing inside its vendored repository).

These are excluded from the frozen task list, with the reason recorded, rather
than tolerated. A task the oracle cannot solve adds a constant zero to every arm,
which dilutes real differences and rewards whichever arm retries more.

## Both adapters run against the real endpoint with a real credential

Verified by driving the actual adapter classes, not by reading them:

| Arm | Turn | Tools | Token dimensions |
| --- | --- | --- | --- |
| XHarness | completed, 4.4 s | 4 | five-way parsed |
| upstream `dsh` | completed | 4 | five-way parsed |

Both created the file they were asked to create and read it back, so the agents
are doing work rather than returning text that merely looks plausible.

`tests/smoke_local.py --live` is the reproducible check. It needs a credential in
`DEEPSEEK_API_KEY` and costs a few hundred tokens.

## The verifier-network failure is fixed by egress, verified 4/4

Four tasks that had *all* failed with verifier network errors -- with the
reference solution applied -- were re-run **unmodified**, with working egress as
the only change:

| Task | Before | After | Verifier |
| --- | --- | ---: | --- |
| `db-wal-recovery` | verifier network failure | **1** | 7 passed |
| `merge-diff-arc-agi-task` | verifier network failure | **1** | 5 passed |
| `password-recovery` | verifier network failure | **1** | 2 passed |
| `write-compressor` | verifier network failure | **1** | 3 passed |

The verifier logs show `downloading uv 0.9.5` completing, and **zero** network
errors in every one. That is the mechanism: the verifiers were never testing the
agent, they were failing to fetch their own toolchain.

So the earlier 4/8-on-the-oracle reading was an artifact of the host, not a
property of the task set -- which is exactly why the oracle gate exists.

## The oracle gate has been exercised, and it failed

Quantified on a full run over the published task images: **4 passed, 4 failed, of
which 3 failed to a verifier network error**, with the reference solution
applied. The floor should be ~100%. The details, the two designs that were
tested, and why `--from-prebuilt` is the correct one are in
`docs/findings.md`.

Running the `oracle` baseline is not a formality here. On the first real attempt
the reference solution scored **1/5**, both failures being verifier-side network
errors before any test ran. That is the gate doing its job: the task set is not
validated on this host, so no harness comparison could have been trusted.

The response is `prepare_tasks.py`, which bakes the verifier's toolchain into the
image so grading needs no network. Whether that restores oracle to ~100% is the
open question -- see the run log, not the design.

## The adapter is verified against the real binary

`tests/smoke_local.py` drives the actual `XHarnessAgent` class against a real
`xharness-host` on the local machine -- no container, no credential, no
Terminal-Bench. It supplies a duck-typed `BaseEnvironment` (the adapter only ever
calls `exec`, `upload_file` and `upload_dir`) and a fake SSE provider that reports
known token counts, so the whole control-plane sequence runs in seconds.

Result: **10/10 checks pass**, including all five token dimensions arriving
intact and `turn/end` reporting a clean `completed`.

This covers exactly what the oracle baseline cannot: the oracle never invokes an
adapter, so a broken adapter would show up only as a mysterious low score
attributed to the harness. It does *not* show that a real model solves anything --
that needs the benchmark.

The desktop shell is deliberately out of scope. The adapter extracts
`xharness-host` and the static assets and never runs the AppImage runtime, so the
shipped GUI's EGL failure on Mesa 26 has no bearing on these measurements.

## Not yet done

1. **No Terminal-Bench trial by an adapter.** Harbor has been driven end to end,
   `oracle` has been run against real tasks, and the adapter is verified against
   the real host locally. What has never happened is an adapter completing a
   *benchmark* trial -- which needs the oracle gate to pass first.
3. **No task-hash freezing in the run loop.** `provenance.py` implements the
   freeze/verify step and is tested, but nothing yet calls `verify()` from the
   runner -- a run still has to be checked by hand.
4. **No cost source.** `cost_usd` is plumbed through `AgentContext` but no price
   table is wired up, so `cost_per_solved_task` cannot be computed yet.
5. **`tasks/` is empty.** Held-out private tasks (countermeasure T3) are not
   written.

## First three commands to run

```bash
# 1. tasks and verifier are sound at all
harbor run -d terminal-bench@2.0 --agent oracle --n-concurrent 4

# 2. the verifier actually fails an empty agent
harbor run -d terminal-bench@2.0 --agent nop --n-concurrent 4

# 3. the XHarness adapter completes one task
harbor run -d terminal-bench@2.0 --agent nop --n-concurrent 1 \
  --agent xharness_bench.agents.xharness:XHarnessAgent --model deepseek/deepseek-v4-flash
```

If (1) does not score near 100%, or (2) does not score near 0%, stop -- the
table is broken and no harness comparison on it means anything.