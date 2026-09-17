# Threats to validity

Each threat, and the specific countermeasure this repository implements. A
comparison that ignores any of these produces numbers that look precise and mean
nothing.

## T1 -- Non-determinism

The same model on the same task does not always produce the same outcome.

**Countermeasure.** Randomise *tasks*, not repetitions. A paired design over
200 tasks with one run each carries far more information than 20 tasks run 10
times, because task difficulty dominates run-to-run variance. Analysis is paired
per task (McNemar), never pooled.

**Residual risk.** A single run per task cannot separate "the harness failed"
from "the model had a bad draw". Report per-task outcomes so this is visible,
and repeat only the discordant pairs if a tie-break is needed.

## T2 -- Temporal drift

Provider latency moves, models are silently updated, rate limits change.

**Countermeasure.** Interleave arms: A, B, A, B -- never A, A, ..., B, B.
Blocked ordering converts any drift over the run into a difference between arms.
Record wall-clock timestamps for every trial and check for a trend before
trusting a result.

## T3 -- Contamination

Public benchmarks are plausibly in pretraining data.

**Countermeasure.** Keep a held-out private task set whose solutions are never
published, and report it separately. Run a canary: strip the solution from a
public task and check whether the agent behaves as if it remembers the tests.
Terminal-Bench is newer and less likely to be contaminated than SWE-bench, but
"less likely" is not "verified".

## T4 -- Budget caps as a confound

If one harness hits a token or turn cap and another does not, the cap is
measuring, not the harness.

**Countermeasure.** Set generous caps, equal across arms, and report the
distribution of actual consumption alongside the censored count. A result driven
by an arm hitting the ceiling must be reported as `budget_exhausted`, not as a
loss.

## T5 -- Winning by spending

A harness can buy pass rate with tokens.

**Countermeasure.** Pass rate is never reported alone. Report
`tokens_per_solved_task` and `cost_per_solved_task`, and plot the
cost/pass-rate frontier. Dominance, not raw score, is the interesting relation:
a harness that is cheaper *and* passes more is unambiguously better; one that
passes 3 points more for 4x the cost has not obviously won.

## T6 -- Asymmetric observability

XHarness reports five token dimensions (uncached input, output, cache read,
cache write, reasoning). Other harnesses report fewer, or define them
differently -- some include reasoning in output, some count cache reads as
input.

**Countermeasure.** The primary metric is externally verified pass/fail, which
no harness self-reports. Token data is recorded for everything and compared only
within Tier A, where both arms run the same accounting code.

## T7 -- Test tampering

An agent can pass by editing the tests.

**Countermeasure.** Verify on a pristine checkout: apply only the agent's diff to
a clean tree, restore all test files from the reference, then run. Record the
hash of test files before and after; a change means the trial is
`verifier_rejected`, not a pass. This is a correctness requirement, not a
nicety -- without it the metric is trivially gameable.

## T8 -- Misattribution

A pass rate cannot distinguish "this harness is good" from "this model is good"
or "the task was easy".

**Countermeasure.** The oracle / nop / mini-swe-agent triad described in the
README. Oracle bounds the task set from above, nop from below, and mini-swe-agent
establishes what a trivial scaffold achieves. A harness that does not clear
mini-swe-agent has not earned its complexity.

## T9 -- Adapter bugs masquerading as harness quality

The adapter is code we wrote; its defects would be scored as the harness's.

**Countermeasure.** Smoke-test each adapter against a known-trivial task, and
assert that the adapter adds no behaviour the harness lacks -- no extra retries,
no output repair, no prompt massaging beyond the documented, identical scaffold
applied to every arm. Version-pin and record every adapter revision alongside
its results.

## T10 -- Selective reporting

Running many tasks and publishing the favourable subset.

**Countermeasure.** Freeze the task list and publish its hash before running.
Report every task, including infrastructure failures, which are classified
rather than dropped.

## T11 -- Host load is a confound for tasks with timing assertions

Some Terminal-Bench tasks assert *relative performance*, not just correctness.
`largest-eigenval`'s `test_speedup` is documented as "Make sure new
implementation is faster than reference" and compares isolated timings; five
tasks in the suite carry assertions of this kind (`cancel-async-tasks`,
`largest-eigenval`, `portfolio-optimization`, `query-optimize`, `tune-mjcf`).

Observed directly: running the oracle gate while a parallel image prefetch
saturated the disk and CPU produced

```
FAILED ../tests/test_outputs.py::test_speedup[8] - AssertionError: 0.000019 s
26 passed
```

-- 26 tests passing and exactly one timing assertion failing, on the reference
solution, which should score 100%. The task set was fine; the machine was busy.

**Why this is worse than ordinary flakiness.** It is not random. Load is
*correlated with arm and with run order*: whichever arm happens to run while
something else is happening on the host collects the failures. In an interleaved
schedule the damage is spread, but any concurrent work -- a prefetch, a build, a
second experiment -- biases towards whoever runs during it. A comparison run
alongside anything else is measuring the host, not the harness.

**Countermeasures.**

1. **Run the gate and the comparison on a quiet host.** This is the real fix.
   Nothing else heavy runs concurrently, and the prefetch completes first.
2. **Re-run timing-sensitive failures alone.** The five tasks above are
   identifiable by inspection, so a failure there under load is re-run rather
   than counted.
3. **Never let a load-induced failure enter a pass rate.** It belongs in
   `harness_error`, and `report.classify` already has that bucket -- but the
   detection here needs the test name, not just the reward, so it is currently a
   manual step and is listed as such in `docs/status.md`.

## T16: the two harnesses do not mean the same thing by "context window"

Reading the failure composition of the 47x2 run showed a clean threshold. Cumulative
reasoning tokens per trial:

| outcome | reasoning tokens (~) |
| --- | --- |
| completed (7 trials) | 2,100 to 14,600 |
| `error` (5 trials) | 30,300 to 42,900 |
| `max-tokens` (2 trials) | 98,400 and 109,500 |

and the boundary sits exactly at the output budget the run was pinned to. The host's
own error says why, once it is asked:

```
invalid token budget: output reserve (131072) plus safety margin (1024)
must be smaller than context window (131072)
```

So in XHarness, `--max-output-tokens` is not an output cap beside the context
window; it is **carved out of** it. A request is refused when the remaining input
budget cannot hold the conversation, the host attempts compaction, and if that
cannot help the turn ends as a budget failure rather than a task failure.

Then the two defaults were measured rather than assumed:

| | declared capacity for `deepseek-v4-flash` |
| --- | --- |
| upstream dsh (runtime shipped config) | `contextWindow: 128e3` |
| XHarness (own model profile) | `fallback_context_window_tokens: 1000000` |
| the model itself | at least 1,000,000 (1,000,031 input accepted, no error) |

Neither harness ships the model's real capacity, they disagree with each other by
8x, and the same flag name means different things on each side. `max_tokens` on
upstream is an output cap; `--max-output-tokens` on XHarness is a claim on the
context.

**What was done about it.** The smaller of the two declared capacities is used, so
neither arm gets more room than the other: 128,000 for both. That this happens to be
close to the 131,072 the run had been using is luck, not design -- the earlier value
came from an unverified comment.

Both arms emit `request/context` carrying the `contextWindow` they actually ran
with, so the effective value is now read back per trial instead of assumed from the
flags. Assuming it is what produced this threat in the first place.

**What is still not equalised.** XHarness reserves its output budget out of the
window and the upstream reserve semantics are not documented anywhere reachable, so
the two arms' *effective input* budgets are probably not identical even at the same
declared window. This is reported rather than silently normalised: the effective
numbers are in every result row, and any conclusion is stated against them.

## T15: the oracle gate and the comparison have different load profiles

The gate that validated the task set ran the oracle, which runs the reference
solution and exits. The verifier then has the box largely to itself. The comparison
runs two LLM agents that work for minutes at a time, so the verifier contends with
them. Passing the gate therefore does not establish that a task can *register* a
harness difference under the conditions the comparison runs in.

This is not hypothetical. Five of the 47 frozen tasks assert on wall-clock time, and
one of them failed in the first comparison run for that reason:

```python
assert dt < ref_dt, f"{dt:.6f} seconds/call > {ref_dt:.6f} seconds/call"
```

`largest-eigenval` compares the median runtime of the candidate against the median
runtime of the reference, with no margin, over matrices small enough that both sides
are measured in microseconds. On an idle host it passes -- verified three times in a
row, and it passed the gate. Under two concurrent trials it failed at 12 to 25
microseconds, which is noise dominating the comparison rather than a difference in
the candidate.

The same task's verifier has been observed failing the oracle once at 0.000019 s and
passing three consecutive times afterwards, so the instability is in the task, not in
either arm.

**Control.** `scripts/oracle-control.sh` runs the oracle over the frozen set with the
comparison's own flags -- same mounts, same timeouts, same `-n` -- and costs no
tokens. Any task the oracle fails there cannot have a harness difference attributed
to it, and is reported separately rather than folded into a pass rate.

The five timing-sensitive tasks, found by grepping the frozen set's verifiers for
`elapsed|perf_counter|time.time|duration|speedup|timeit|benchmark`:

```
cancel-async-tasks            2
constraints-scheduling        1
largest-eigenval              4
make-mips-interpreter         2
schemelike-metacircular-eval  2
```

These are not excluded a priori. Excluding them on suspicion would be its own
distortion; the control decides, per task, with evidence.

## T14: unequal output budgets between arms

Both adapters defaulted their output cap to "whatever the implementation chooses",
which is not a controlled quantity. In the first attempt at the comparison, two of
three XHarness trials ended with `turn_end_reasons: ["max-tokens"]` after fewer than
60 seconds and four tool calls, while the third completed normally. A truncated turn
scores zero, so the arm with the smaller default loses trials for a reason that has
nothing to do with the harness being compared.

Both arms are now pinned to the same explicit budget (`max_output_tokens=32768` for
XHarness, `max_tokens=32768` for the upstream SDK), the same context window, and the
same agent timeout. The first three trials after pinning all completed with reason
`completed` and no truncation.

**The general point.** "Use each implementation's defaults" sounds neutral and is
not: defaults are part of the implementation, they differ, and where they differ
they decide trials. Any budget that can end a turn early has to be pinned across
arms and recorded.

## T12 -- Measure one thing at a time: bulk transfers starve the verifiers

This is the most expensive mistake made while building this, and it produced a
completely wrong answer that looked like a real one.

**What happened.** The oracle gate was launched while task images were still
being pulled. Both paths share one proxy. The result, over 48 tasks:

```
9 passed / 39 failed  = 18.8%
```

with 35 of the 39 failures identical: `uvx: command not found`, meaning the
verifier never managed to download its own toolchain. The very same task set, run
with the machine otherwise idle, gave **12/13 = 92.3%** and climbing.

**The mechanism**, measured directly rather than reasoned about. With a single
8 GiB image layer in flight, from the two places that matter:

| Where | Download of the 21 MB uv binary |
| --- | --- |
| host, through the proxy | **180 KB in 60 s** (3 KB/s, then timeout) |
| inside a container, same proxy | 21 MB in 16 s (1.3 MB/s) |

and in the verifier logs, `curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in
connection to astral.sh:443`. Meanwhile `apt`, which is listed in the daemon's
`NO_PROXY` and therefore bypasses the proxy, kept working at 985 kB/s inside the
same containers. So the failure was specific to proxy-routed TLS under saturation,
not to the network as a whole.

**Why this is a trap rather than an obvious blunder.** Every individual piece
looked healthy. The proxy node answered its own latency probe in 61 ms. The
registry mirror responded. DNS resolved. `apt` succeeded. A single `curl` to
`astral.sh` returned `301`, which reads as success unless you follow the redirect
to the GitHub asset that actually carries the bytes. Nothing announces "you have
saturated the link" -- the symptom is that *another* subsystem's TLS handshakes
get reset, in containers, minutes later.

**Countermeasure, and it is mechanical rather than a matter of care.**
`scripts/quiet-gate.sh` refuses to start a gate while a `docker pull` is running
or while the proxy shows more than a handful of active connections. A rule that
depends on remembering is not a rule. This is the same discipline as T11 -- one
heavy thing at a time -- but the failure mode is worse: T11 corrupts a few timing
assertions, T12 corrupts the entire pass rate in a direction that looks like a
broken task set.

**How it was caught.** Only by the oracle baseline. A harness comparison run in
that state would have reported a plausible, publishable, entirely false result,
and no amount of statistical care downstream would have detected it. This is the
strongest argument in this repository for the oracle gate being load-bearing
rather than ceremonial.

## T13 -- Task bit rot, and why the oracle gate is not a 100% requirement

The oracle gate's job is not to score 100%. It is to **identify which tasks are
broken** so they can be excluded before a comparison. The valid task set is the
one where the reference solution passes; a requirement of "100% on everything
ever published" would make the gate unpassable for reasons that have nothing to
do with the benchmark.

Measured on 2026-09-15 against a task set published 2025-10-31, four failures --
**all four task-side, none environmental**:

| Task | Failure | Root cause |
| --- | --- | --- |
| `build-pov-ray` | `povray.org` returns 403 | the URL is dead; **direct and proxied both return 403**, same 5564-byte body, so it is not the egress path |
| `make-doom-for-mips` | `apt` cannot fetch `libpng16-16_1.6.39-2`, `libtiff6_4.5.0-6+deb12u2`, ... | Debian removes superseded pool entries; `curl` on that exact `.deb` URL returns 404 today |
| `build-pmars` | `E: Version '1.22.21' for 'dpkg-dev' was not found` | the pinned version has left the archive |
| `build-cython-ext` | `test_reconstructed_space_curve` fails inside the vendored repository | upstream code, unrelated to the harness |

The pattern is that these tasks pin **external state that expires**: exact package
versions, a specific third-party URL, a vendored dependency's own test suite. Near
the publication date they pass; a year later some of them cannot.

**Why this must be handled by exclusion rather than tolerance.** A task that fails
for the oracle fails for every arm. Including it does not add noise evenly -- it
adds a *constant* zero, which dilutes any real difference and, worse, rewards
whichever arm happens to retry more or to reach a mirror that still has the
package. The task is not measuring the harness, so it must not appear in the
denominator.

**Procedure.** Run the gate; classify every failure as environmental or task-side;
freeze the task list from the tasks whose oracle passed; publish that list and its
digest (countermeasure T10). Tasks that fail here are recorded as excluded, with
the reason, rather than silently dropped.

**Note the interaction with T12.** An environmental failure and a contention
failure look identical in the output -- both are "oracle scored 0". Telling them
apart required reading the verifier logs and re-testing the URLs directly. That is
why the gate must run on a quiet host: with contention in play there is no way to
distinguish "this task is broken" from "this run was starved".

## Resolution floor

From `report.tasks_required`: detecting a 5-point difference at 80% power needs
roughly 780 paired tasks. Terminal-Bench 2.0 provides on the order of 100. The
honest consequence is that this suite can only resolve **large** effects, and
"no significant difference" is the expected outcome for closely-matched
harnesses. Any write-up must state the resolution floor next to the result.