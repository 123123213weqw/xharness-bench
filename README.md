# xharness-bench

Controlled comparison of agent harnesses on [Terminal-Bench](https://github.com/laude-institute/terminal-bench),
with **XHarness** as the subject.

The interesting question is not "which harness is best" -- that is not
answerable, because every harness bundles a different system prompt, tool set
and loop, and those *are* the object of study. The answerable question is:

> Holding the model, the task, the workspace and the prompt fixed, what does a
> given **implementation** change?

For XHarness specifically there is a rare opportunity to ask that precisely.

## The opportunity: a built-in control group

XHarness (`x-harness-rs`) is a Rust re-implementation of
[`deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness),
and it tracks that upstream deliberately. The repository ships a frozen
compatibility catalog:

| Surface | Upstream | XHarness covers |
| --- | ---: | ---: |
| Fixed RPC methods | 52 | 52 |
| Mux frames | 10 | 10 |
| Host frames | 10 | 10 |
| Session events | 48 | 26 |
| Static tools | 53 | 15 |

Frozen at `deepseek-harness@141eb6fef8`, which is upstream tag
**`dsh-v0.1.0-rc.8`** -- a tag, so the reference is reproducible rather than a
moving branch.

Both sides speak the same control plane. So the same task can be driven through
a faithful replica and through the original with the prompt and tool surface
held constant. That isolates implementation: language, concurrency model,
context management, transport. **This control group cannot be constructed for
any other harness**, which is why the primary experiment runs here.

## Two tiers

| | Tier A -- replica fidelity | Tier B -- cross-harness |
| --- | --- | --- |
| Arms | XHarness vs upstream `dsh` | + Claude Code, Codex, OpenHands, mini-swe-agent |
| Held constant | model, task, workspace, prompt components, tool surface | model, task, workspace |
| Free variable | the implementation | the whole agent design |
| Primary metric | externally verified pass/fail | externally verified pass/fail |
| Token accounting | comparable | **recorded, never compared** |
| What it can support | "the replica differs from upstream by X" | "these products differ by X on these tasks" |

Tier B is deliberately weaker and must be reported as such: Claude Code's system
prompt is not editable, so it is part of the treatment, not a nuisance. Only
externally verified outcomes are comparable across Tier B arms. Self-reported
token counts are not, because harnesses count differently (whether reasoning
tokens are included, whether cache reads are billed as input, and so on).

## What this repository does *not* build

Orchestration is [Harbor](https://github.com/laude-institute/harbor)'s job, and
it already provides almost everything:

| Needed | Already in Harbor |
| --- | --- |
| Task sandbox, container lifecycle | yes |
| Upper bound / sanity check | `--agent oracle` |
| Lower bound | `--agent nop` |
| **Naive-loop baseline** | `--agent mini-swe-agent` |
| Tier B commercial agents | `claude-code`, `codex`, `openhands`, `gemini-cli`, `qwen-coder`, ... |
| Parallelism, retries, result store | yes |

Re-implementing any of that would be wasted effort. What is missing, and what
lives here:

1. an agent adapter for **XHarness** (`src/xharness_bench/agents/xharness.py`),
2. an agent adapter for **upstream dsh** (`.../agents/dsh_upstream.py`),
3. the **controls** that make the comparison fair and reproducible,
4. the **analysis** that keeps the conclusion honest (`report.py`).

## Status

The paired comparison has run. Full write-up in [`docs/results.md`](docs/results.md);
the headline is that **this task set cannot resolve the difference between the arms**:

| arm | version | pass |
| --- | --- | ---: |
| XHarness | 0.2.19 (replica of `dsh-v0.1.0-rc.8`) | 35/47 = 74.5% |
| upstream dsh | SDK `0.1.0rc7` | 34/43 = 79.1% |

Paired over the 43 tasks both arms were graded on: 31 both pass, 6 both fail, 3 and 3 the
other way. Discordant 6, exact McNemar **p = 1.0000**. 194 tasks would be needed to
resolve a 10-point difference.

What the same data does support is cost, which is not subject to small-effect problems:
XHarness's median trial is 470 s against upstream's 224 s, and its prompt per step is 2-3x
larger on tasks both arms passed.

Three things to know before reading any number here:

- **It is about `0.1.0rc7`, which is sixteen tags old.** Upstream has since shipped
  `dsh-v0.1.6-alpha.1` with breaking changes to the session format, the API surface and
  the default provider protocol. See [`docs/version-drift.md`](docs/version-drift.md).
- **86% of the tasks cannot discriminate** — 31 of 43 pass for both arms, 6 fail for both.
  The effective sample is the 6 discordant pairs, not 43 tasks.
- **26 validity threats were found and fixed along the way**, each of which had produced a
  plausible-looking wrong number. They are enumerated in
  [`docs/validity.md`](docs/validity.md). One of them (T26) favoured the replica, which is
  noted there for the same reason as the rest.

The environment is provisioned end to end; `docs/status.md` keeps the per-stage account.

## Quickstart

Harbor needs Docker. On this project's host the container registry required a
mirror, because `registry-1.docker.io` resolved to an unreachable IPv6 address
while mirrors resolved normally -- see [`docs/findings.md`](docs/findings.md).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[analysis]"
pip install harbor

# sanity: the adapter is importable and Harbor can load it
python -c "from xharness_bench.agents import XHarnessAgent; print(XHarnessAgent.name())"
harbor run --help
```

Tier A, both arms on the same tasks:

```bash
# oracle first: it proves the task set and verifier work at all
harbor run -d terminal-bench@2.0 --agent oracle --n-concurrent 4

# then the paired comparison
harbor run -d terminal-bench@2.0 \
  --agent xharness_bench.agents.xharness:XHarnessAgent \
  --model deepseek/deepseek-v4-flash --n-concurrent 4

harbor run -d terminal-bench@2.0 \
  --agent xharness_bench.agents.dsh_upstream:DshUpstreamAgent \
  --model deepseek/deepseek-v4-flash --n-concurrent 4
```

Aggregate:

```bash
python -m xharness_bench.report --results runs/ --baseline dsh-upstream --candidate xharness
```

## Making the run possible: bake the verifiers' dependencies

Terminal-Bench 2.0 verifier scripts acquire their own toolchain at grading time
-- `uv` from astral.sh/GitHub, then a CPython 3.13 toolchain and pytest from PyPI
via `uvx`, tens of megabytes per trial, before any test runs. When that fails the
verifier writes `reward.txt = 0`, which cannot be distinguished from the agent
failing the task. On a host whose connectivity to GitHub is intermittently
degraded this produces false negatives at a rate that swamps the effect being
measured; on the reference host the **oracle scored 1/5**.

`prepare_tasks.py` fixes that without touching the tests:

```bash
harbor download terminal-bench@2.0 -o tasks/terminal-bench

python -m xharness_bench.prepare_tasks \
  --source tasks/terminal-bench \
  --output tasks-baked \
  --uv-version 0.9.5 \
  --manifest tasks-baked/manifest.json

harbor run -p tasks-baked --agent oracle --force-build -n 3
```

It appends one Dockerfile layer that installs `uv` and runs the verifier's exact
`uvx` command once, so the toolchain is already in the image's uv cache, and it
neutralises the download line in `tests/test.sh` with `UV_OFFLINE=1`. The same
tests then run against the same agent output.

The claim that the tests are unchanged is checkable, not asserted: the manifest
records a SHA-256 of `tests/test_outputs.py` for every task, computed before and
after, so a reviewer can confirm the test body is byte-identical and that the
Dockerfile patch is purely additive.

## Baselines are mandatory

A pass rate without a floor is uninterpretable. Every reported comparison must
carry:

- **`oracle`** -- confirms the tasks and verifiers are sound. If oracle does not
  score ~100%, the task set is broken and nothing else can be concluded.
- **`nop`** -- confirms the verifier actually fails an empty agent. A verifier
  that passes `nop` is not testing anything.
- **`mini-swe-agent`** -- the naive baseline. Recent work shows a plain bash
  ReAct loop is competitive on software tasks. If a sophisticated scaffold does
  not beat it, the sophistication is not paying for itself.

Without these three, a harness's score cannot be attributed to the harness.

## Statistical budget

This is the part that decides whether a result is worth anything, so it goes in
the README rather than a footnote. Paired McNemar, α = 0.05, power = 0.80,
assumed discordance 0.25:

| Difference to detect | Paired tasks needed |
| ---: | ---: |
| 10 points | ~200 |
| 5 points | ~780 |

Terminal-Bench 2.0 has on the order of 100 tasks; SWE-bench Lite has 300. So:

- differences below roughly 8-10 points are **not measurable** on these suites;
- "no significant difference" is a legitimate and publishable outcome;
- a 20-task run cannot detect anything worth reporting, and must not be
  presented as if it could.

`report.tasks_required()` computes this so a design can be checked before
compute is spent.

## Layout

```
src/xharness_bench/
  rpc.py                  JSON-RPC client; drives the host over the container's loopback
  agents/xharness.py      XHarness adapter
  agents/dsh_upstream.py  upstream DeepSeek Harness adapter
  report.py               Wilson intervals, exact McNemar, failure taxonomy, aggregation CLI
  provenance.py           freeze the task list and interleave the run order
configs/                  Tier A and Tier B run configurations
scripts/                  the comparison, the controls, and the report CLIs
tests/smoke_local.py      shape checks, run against captured artefacts rather than invented ones
docs/validity.md          threats to validity and the countermeasures
docs/findings.md          facts established while building this, with evidence
docs/status.md            what is verified vs. still assumed
```

## Freeze the plan before measuring

Countermeasure T10 is procedural, so it needs a tool rather than a resolution.
Freeze the task set first and publish the digest; the verification step then
fails loudly if the run measured something else:

```bash
# before running anything
python -m xharness_bench.provenance \
  --dataset terminal-bench --version 2.0 \
  --tasks task-ids.txt --arms xharness,dsh-upstream \
  --output frozen-plan.json

# after collecting the task list actually used
python -m xharness_bench.provenance \
  --dataset terminal-bench --version 2.0 \
  --tasks observed-ids.txt --arms xharness,dsh-upstream \
  --verify frozen-plan.json
```

The digest is order-independent, so it detects tasks being *swapped*, not merely
reordered. Schedule order is interleaved by construction (T2).

## What is deliberately not committed

Two of the things this repository measures against are *outputs* of a run on the
reference host rather than source, so they are absent from a clean clone:

- `tests/fixtures/` -- the two captured sessions the shape checks assert on.
  `scripts/capture-fixtures.sh` re-captures both from the host.
- `frozen/tier-a-plan.json` -- the frozen task list and its digest, written by the
  command above before a run.

That absence is visible rather than silent: with no fixture, `tests/smoke_local.py`
reports `FAIL  real session fixture is present` instead of passing while measuring
nothing. `scripts/quiet-gate.sh`, which `scripts/oracle-control.sh` calls to enforce
T12, is in the same category and is listed in [`docs/status.md`](docs/status.md).

## License

MIT.