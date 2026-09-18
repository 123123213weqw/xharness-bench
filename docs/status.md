# Status

What is verified, what is assumed, and what remains. Kept current rather than
optimistic.

**As of the last commit:** the paired comparison has run and is written up in
[`docs/results.md`](docs/results.md). The sections below are the per-stage account of
how the environment got there, void stages included. Where a present-tense sentence
below conflicts with `results.md`, it is history rather than status.

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

## Tier A, attempt 1: void

The first 47x2 run completed and wrote a full set of results, and the results are
not usable. 19 of 94 trials got as far as the agent; the other 75 died in setup
because both adapters assumed the task image would supply a runtime. One arm
reported 0% and the other 46%, and neither number is about the harness.

Kept as `runs/_void-tier-a-*`, because the failure mode is worth being able to point
at: 94 result files, a job summary, sensible-looking pass rates, and no agent
execution behind most of them.

Attempt 2 ran with both setups rewritten to use the `uv` that every baked image
carries, and with `--agent-setup-timeout-multiplier 4`; it is the run written up in
[`docs/results.md`](docs/results.md).

## Gate result: 44/57 with zero harness-side failures

Run on a quiet host, with the cache bind-mounted and every task baked:

| | count |
| --- | ---: |
| passed | **44** |
| failed for task-side reasons | 10 |
| failed for a cache gap in this setup | 3 |

**Zero failures from verifier networking, uvx, or contention** -- which was the
entire purpose of the baking and mounting work. Earlier attempts on the same task
set scored 18.8% (contention), 7% (baked images silently unused) and 80.6%
(overnight network outage), all of which were environmental.

The 13 non-passing tasks, and why each is excluded or retried:

| Task | Reason |
| --- | --- |
| `torch-tensor-parallelism`, `torch-pipeline-parallelism`, `mteb-retrieve` | the warmed cache lacks `torch`; being re-warmed and re-run |
| `build-pmars` | Debian removed the pinned `dpkg-dev=1.22.21` |
| `build-pov-ray` | the source URL returns 403 to direct and proxied requests alike |
| `make-doom-for-mips` | Debian removed the pinned pool entries |
| `build-cython-ext` | a test fails inside its vendored repository |
| `caffe-cifar-10` | the model file is not produced by the reference solution |
| `configure-git-webserver` | the reference solution's server does not come up |
| `count-dataset-tokens` | the reference solution does not write `/app/answer.txt` |
| `mcmc-sampling-stan` | the RStan/Stan check fails |
| `fix-code-vulnerability` | its verifier uses plain `pip`, outside the uv cache |
| `mailman` | its verifier uses `uv venv`, outside the uv cache; being warmed and re-run |

Note that two of these are gaps in **this** setup rather than in the tasks: five
verifiers use plain `pip` and one uses `uv venv`, and the baking only covers
`uvx`. Those are being closed rather than blamed on the task set.

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

1. **Nothing calls `provenance.verify()` from the run loop.** The freeze/verify step
   exists and is tested, but a run still has to be checked against the frozen plan by
   hand.
2. **No cost source.** `cost_usd` is plumbed through `AgentContext` but no price table
   is wired up, so `cost_per_solved_task` cannot be computed yet.
3. **`tasks/` is empty.** Held-out private tasks (countermeasure T3) are not written.
4. **Three referenced artefacts are not in the repository**, because they are outputs of
   a run on the reference host rather than source:

   | Referenced by | Not present | Where it comes from |
   | --- | --- | --- |
   | `tests/smoke_local.py`, T22 | `tests/fixtures/real-session-xharness.json`, `real-session-durable.json` | `scripts/capture-fixtures.sh`, run on the host |
   | `docs/results.md`, `scripts/oracle-control.sh` | `frozen/tier-a-plan.json` | `python -m xharness_bench.provenance --output frozen/tier-a-plan.json` |
   | `scripts/oracle-control.sh`, T12 | `scripts/quiet-gate.sh` | written on the host while the gates ran and never committed; recover it from there, or rewrite it against the contract in T12 |

   The first is deliberate and visible: with no fixture the shape checks report
   `FAIL  real session fixture is present` rather than passing while measuring nothing.
   This is also why a fresh clone's failure count is not the host's.

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