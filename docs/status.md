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

## Assumed, not yet verified

| Assumption | Risk if wrong |
| --- | --- |
| The adapter's RPC sequence matches the live host | the run fails at the first call; the sequence is copied from the in-repo driver so this is likely but untested *through Harbor* |
| `ExecResult` exposes `return_code`/`stdout` | normalised in `rpc.exec_parts` to tolerate either naming, but untested |
| Task containers permit installing `curl` | `setup()` tries apt/apk/dnf/yum and fails loudly; minimal images without a package manager would need a bundled client |
| The upstream SDK version matching `dsh-v0.1.0-rc.8` exists on PyPI | the Tier A upstream arm cannot be installed; resolve before running |
| Terminal-Bench task images reach a package mirror | `setup()` would fail; a task set with vendored dependencies avoids this |

## Not yet done

1. **No end-to-end trial.** Nothing in this repository has been run against a
   Terminal-Bench task through Harbor. The first milestone is a single task with
   `oracle`, then a single task with the XHarness adapter.
2. **No `report` CLI.** `report.py` has the statistical functions; the
   command-line entry point referenced in the README is not written yet.
3. **No task-hash freezing.** The provenance step that hashes and publishes the
   frozen task list (countermeasure T10) is designed but not implemented.
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