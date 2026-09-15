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

## Resolution floor

From `report.tasks_required`: detecting a 5-point difference at 80% power needs
roughly 780 paired tasks. Terminal-Bench 2.0 provides on the order of 100. The
honest consequence is that this suite can only resolve **large** effects, and
"no significant difference" is the expected outcome for closely-matched
harnesses. Any write-up must state the resolution floor next to the result.