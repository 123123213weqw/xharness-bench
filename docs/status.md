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

## The oracle gate has been exercised, and it failed

Running the `oracle` baseline is not a formality here. On the first real attempt
the reference solution scored **1/5**, both failures being verifier-side network
errors before any test ran. That is the gate doing its job: the task set is not
validated on this host, so no harness comparison could have been trusted.

The response is `prepare_tasks.py`, which bakes the verifier's toolchain into the
image so grading needs no network. Whether that restores oracle to ~100% is the
open question -- see the run log, not the design.

## Not yet done

1. **No end-to-end trial of an adapter.** Harbor has now been driven end to end
   and `oracle` has been run against real tasks, but no *adapter* from this
   repository has completed a trial yet. The XHarness adapter is still unproven
   through Harbor, which is the first thing to establish once the oracle gate
   passes.
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