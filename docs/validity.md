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

## T25: `--agent-timeout-multiplier` never applied, because both adapters cap earlier

Both adapters carry `turn_timeout_sec: int = 1800` and enforce it themselves. Harbor's
`--agent-timeout-multiplier 4` scales Harbor's own limit, which the adapters never reach,
so the effective cap on an agent turn was 1,800 seconds regardless of the flag.

`extract-moves-from-video` is where it shows. Both arms die at the cap and neither is
recorded as a timeout by the adapter:

```
xharness  1801.8 s  reasons []          (loop exited on the wall clock, no turn/end)
upstream  1800.0 s  Command timed out after 1800 seconds
```

Symmetric, so it is a bound rather than a bias -- the same shape as T18. It does mean
every task needing more than half an hour of agent time is scored as a failure for both
arms, and the `-4` on the command line did nothing. Raising it requires rerunning both
arms, since a cap that binds one and not the other is worse than one that binds both.

Worth noting how it was found: not by reading the flag, but by asking why a task failed
in both arms at suspiciously round numbers.

## T24: the verifier needs network, and its absence is not visible in the result

Task verifiers run `apt-get install` before their tests. In 62 trials across both arms the
install failed:

```
Err:1 http://deb.debian.org/debian bookworm InRelease
  Could not connect to deb.debian.org:http:
E: Unable to locate package curl
```

39 of those 62 still passed, so for most tasks the missing package is incidental -- the
verifier installs a tool it does not end up needing. But the failure is silent in the
result: a task that fails *because* its verifier could not install something is recorded
as an ordinary `0.0`, indistinguishable from an agent that got the answer wrong. The
`test-stdout.txt` is the only place the cause appears.

Affected both arms roughly equally (21 / 21 / 19 / 1 across the four run directories), so
it is not a bias between them -- it is noise that inflates the failure count on both
sides and shrinks the discriminating set further (T23).

**A near-miss while diagnosing this.** The first measurement said containers could not
reach the proxy at all, because the check was `(echo > /dev/tcp/$ip/7890)` run through
`sh`. That construct is a bash feature; dash does not implement it, so it fails for every
address and reports a total network outage that does not exist. Re-tested with a socket
from Python: `172.20.0.1:7890` reachable, and direct connections to `deb.debian.org:80`
and `pypi.org:443` both succeed. The false alarm was one command away from being written
up as a finding, and the pattern is the same one as T19, T21 and T22 -- a check whose
failure mode looks like the thing it is checking for.

**One task's timeout is the agent's own doing.** `model-extraction-relu-logits` ended with
`Verifier execution timed out after 1800.0 seconds`. Its verifier re-runs the agent's
script to prevent reward hacking:

```python
os.popen("python3 /app/steal.py").read()
```

The script the agent left behind hangs when re-run, which the live container confirms:
`/app/steal.py` is still consuming CPU long after the agent's turn ended. The re-run
exists to stop a solution that hard-codes the answer; it also, correctly, fails a solution
that only works once.

## T23: most of the task set cannot discriminate, and the gate guaranteed that

The 47-task set was filtered by requiring the oracle to pass three times, which removes
anything unsolvable or ambiguously specified -- correctly, since a task nobody can pass
tells you nothing. The consequence is a set whose ceiling is low, and it shows in the
paired result:

```
both pass     26 / 37     70%
both fail      6 / 37     16%
discriminating 5 / 37     14%
```

**86% of the tasks contributed nothing to the comparison.** The effective sample size is
5 discordant pairs, not 37 tasks, and the binomial machinery in report.py operates on the
5.

This is the third distinct reason the comparison is underpowered, and they compound:

1. **T-set size.** 194 tasks are needed for a 10-point difference at 80% power. There are
   47.
2. **Discriminating fraction.** Of those 47, 14% separate the arms, so the usable count is
   nearer 6 than 47.
3. **The budget defect (T16).** For most of the run the arms were not given the same
   context, which pushed failures into a category that says nothing about either harness.

The first two are properties of the task set and could be fixed by using one written for a
narrower skill gap -- a set where a strong agent scores near 100% and a weak one near 0%
would need far fewer tasks, but would only measure large differences, which is the
opposite of what a fidelity check wants.

**What this forbids.** Reporting "74.5% vs 75.7%" as a result. The honest form is the
paired table plus the discordant count plus the power statement, which is what
docs/results.md does.

**What it permits.** Statements about cost, which are not subject to the same problem:
XHarness's median trial is 2.4x longer (470s against 195s) and its prompt per step is
2-3x larger. Those are 2-3x effects measured on 37 paired samples, not 2-point effects
measured on 5.

## T22: a test written from the same assumption as the code certifies the assumption

The tool measurement added for T21 was wrong for both arms, and the smoke test passed.

The code read the tool name from the nested shape the Rust enum implies:

```python
call = data.get("call") if isinstance(data.get("call"), dict) else {}
name = call.get("name")
```

The wire is flat and camelCase instead:

```json
{"type": "tool/call", "data": {"name": "bash", "callId": "xh-...", "arguments": "..."}}
```

So `tool_calls_by_name` came back empty, `tool_result_bytes` was 0, and the rendered
trace printed `CALL ? None` and `result ? 0B` for every step -- the trace showed that a
run had happened and nothing about what it did.

The test did not catch it because the test built its events from the same belief:

```python
def call(idx, name): return {"type": "tool/call", "data": {"call": {"id": ..., "name": name}}}
```

Twelve assertions, all green, against input invented to match the parser. This is worse
than having no test: it produced the evidence that the measurement worked.

**Fix, in two parts.**

1. Extraction moved into `tool_call_name` and `tool_result_text`, which accept both
   shapes, and both the summary and the trace renderer call them -- one implementation
   rather than two chances to drift.
2. The test is built from `tests/fixtures/real-session-xharness.json`, an actual
   80-event session captured from a trial, with its expected values *read off the
   fixture* rather than chosen: 9 calls, all `bash`, 14,236 result bytes, largest 8,171,
   10 provider calls, window 1,000,000. If the fixture were regenerated with a different
   session those numbers would change, and the diff would say so.

**The general rule this is the second instance of.** T19 was also a case of reading a
structure and assuming its shape -- a reply that looked complete, a field that looked
absent. The check that works is not a test of the parser but a captured sample of the
thing being parsed. Where a real artifact can be committed, it should be, and the
expectations should be read from it.

## T21: the tool-call counter was counting results as calls

Every tool-call figure this repository has reported was about twice the truth. The
counter matched any event whose type begins with `tool/`:

```python
elif isinstance(kind, str) and kind.startswith("tool/"):
    tool_calls += 1
```

A tool invocation emits `tool/call` and then `tool/result`, so a run making N calls
counted about 2N. `tool/code-dispatch` and `tool/code-dispatch-start` were caught too,
and those are the runtime executing code rather than the model invoking a tool.

This went unnoticed because the numbers looked plausible. It also compounded with T19:
the 48-to-50 clustering that sent me looking for a step limit was 50 *events* per
default history page, which is about 25 calls, counted twice. Two independent defects
produced a number that looked like a round limit, and the host has no step limit at all.

**Fix.** Only `tool/call` increments the counter; the blanket branch is gone. Rows now
also carry the breakdown that answers the question the total was standing in for:

```
tool_calls_by_name          {"bash": 31, "read_file": 9, "apply_patch": 8}
tool_names_seen             3
tool_result_bytes           184320
largest_tool_result_bytes   41200
tool_results                48
```

A bare count cannot distinguish a run that probes with shell commands from one that
reads files -- both reach the same total -- and it says nothing about context, where
tool output is the dominant term because it is re-sent on every later step.

**Consequence for prior numbers.** Tool-call counts reported before this commit,
including in T19 and in the comparison write-ups, are inflated by roughly 2x and should
not be compared against post-fix rows. Pass rates are unaffected: the verifier grades
the container, not the transcript.

## T20: the comparison ran against a revision the reader will assume is current

The bench names its arms `xharness` and `dsh-upstream`. Neither name carries a
version, and the second one in particular invites the reading "the official harness"
-- present tense, as of today. What actually ran was SDK 0.1.0rc7, published
2026-08-17, which is **one tag older than the contract XHarness replicates**
(`dsh-v0.1.0-rc.8`, 2026-08-19) and 16 tags behind upstream's current
`dsh-v0.1.6-alpha.1` (2026-09-15).

rc.7 was not a careless choice. rc.8 was never published to PyPI, so the adapter's
"nearest installable version" rule lands on rc.7, and the frozen and installed
revisions are probably contract-equivalent -- the delta audit of 2026-08-21 found the
fixed RPC name set unchanged from rc.8 to 0.1.1-rc.2. That makes the pairing a fair
test of *replica fidelity*.

It is not a test of *current competitive standing*, and nothing in the output said so.
The register's own rule applies: a number has to carry its conditions. Failure
classification was added for this reason, so was effective_context_window, so was
turn_end_errors. The version is the same kind of omission.

**Fix.** Every result row now carries the version that produced it, and the pass-rate
table prints it beside the harness name:

```
harness (version)                 pass rate [95% CI]             n    med s
--------------------------------------------------------------------------
dsh-upstream (0.1.0rc7)            76.9% [57.9,89.0] (10 unjudged)  36        -
xharness (0.2.19)                  55.3% [41.2,68.6]            47        -
```

Versions are collected per arm rather than picked, so an arm that ran two revisions
shows both instead of looking like one that ran a single one.

**What would close it.** An arm against the current upstream. `0.1.5rc1` is the latest
PyPI release and installs offline from the warmed cache; the adapter introspects the
SDK config signature (rc7 uses `session_root`, 0.1.5 uses `dsh_home`), so no adapter
change is needed. Until that runs, every conclusion in this repository is a statement
about rc.7 and is written that way.

## T19: the adapter was measuring the last 50 messages and calling it the run

The XHarness arm's tool-call counts clustered at 48 to 50 across unrelated tasks:

```
48 48 48 48 48 49 49 50 50 50 50 50 ...
```

which reads unmistakably as a step limit. The host has none -- `max_steps` is
`usize::MAX` -- and an upstream trial in the same set ran to 248 tool calls, so the
limit had to be on my side. It was:

```python
history = await client.call("session.history", {"sessionId": session_id})
```

`session.history` defaults to `DEFAULT_HISTORY_MESSAGES = 50` when `maxMessages` is
absent, and returns `hasMore` to say the page is partial. The call above passed
neither, and the reply is shaped like a complete history, so nothing about it looks
truncated. Every number derived from it -- token totals, tool-call counts, turn
reasons -- described the tail of the run and was reported as the whole of it.

**Why the pass/fail verdicts survive.** The verifier grades the container, not the
transcript, so the 26 passes and 21 failures in that arm are unaffected. What is
wrong is everything in the right-hand columns, including the token table in T16.

**Fix.** `fetch_history` pages backwards by `beforeSeq` until `hasMore` is false,
using the server's own maximum of 500 messages per request so most runs need one.
Polling still uses the default page -- it only has to answer "has `turn/end`
appeared", and the newest event is always in the tail -- and the complete read
happens once, at the end.

**Evidence, not assertion.** Every row now carries both counts, `tail_events` and
`total_events`. On a run shorter than 50 messages they are equal, which is what the
smoke test asserts; on a long run the gap is exactly the error the un-paged version
was reporting, measured rather than estimated. The regression test builds a
120-step session against a stub that pages the way the server does and checks that
the default page is truncated, that the complete read is not, and that ordering
survives reassembly.

Twice now the fix has been the same shape: ask the system what it did, instead of
inferring it from a reply that happens to look complete.

## T18: the 32,768 output cap binds both arms, symmetrically

The upstream arm hit it too. On `adaptive-rejection-sampler`:

```
turn_end_reasons   ["max-tokens"]
reasoning tokens   32786        cap 32768
tool calls         10           elapsed 496 s
```

Eighteen tokens over. That trial died in a single step having barely started, and it
is the only upstream failure in the run that is not either a wrong answer or a
network error.

So the cap the earlier run was pinned to suppresses both arms, in two different ways:

* XHarness -- the value is a reservation carved out of the context window, so a
  32,768 reserve against the 131,072 window that run used left 97,280 tokens of input
  budget and rejected requests that needed 97,842;
* upstream -- the value bounds a single step, so a step that wanted 32,786 tokens was
  cut off at 32,768.

For the comparison this is **symmetric and therefore not a validity threat in the way
T14 was**: both arms are subject to the same per-step ceiling, so neither is favoured.
It does depress both absolute scores, and the honest reading of any near-tie is that
the cap may be hiding a difference rather than that there is none. It is recorded as
a bound on the measurement, not as a correction to it.

Raising it would require rerunning both arms, since a cap that binds one arm
asymmetrically -- which is what a partial fix produces -- is worse than a cap that
binds both. The current second-run configuration is 1,000,000 context and 32,768
output for both arms, which equalises the input budget to within the safety margin at
roughly 966,000 tokens and the per-step ceiling at exactly 32,768.

The model's own output limit, measured, is 393,216 -- so the cap is 8% of what the
provider would allow. That is the size of the residual.

## T17: provider transport failures are attributed to the harness

Two of the upstream arm's 47 trials ended with

```
error: DeepSeek API request to https://api.deepseek.com failed (TRANSPORT)
```

which is the network, not the harness. The failure arrives as a turn-end reason of
`error` from whichever arm was running, so a reader of the pass rates cannot tell a
harness that failed from a request that never reached the provider. The upstream arm
uses the operator's own proxy, and this is the same proxy path that has needed
attention throughout (T6).

These are not excluded from the reported rates -- they are counted, because excluding
failures that happen to be environmental is how a benchmark flatters its subject. But
they are counted *separately*, by their message, so the two effects can be told apart
in the write-up. The 47x2 comparison cannot resolve an effect this small anyway
(194 tasks are needed for a 10pp difference), so the honest use of these trials is as
a qualitative note about the environment rather than as a term in either arm's rate.

`turn_end_errors` exists because of this: a turn that ends in `error` and a turn that
ends in `error` were indistinguishable in the first comparison run, and the reason was
on the wire the whole time. Both this and the budget rejection above were invisible
until that text was recorded.

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

**The recording caught the inequality, which is the point of recording it.** Running
both arms in the second comparison showed the two on different numbers:

```
xharness      effective_context_window 131072
dsh-upstream  effective_context_window 1000000
```

and the consequences were not subtle. A single upstream trial
(`largest-eigenval`) completed having used 127,805 reasoning tokens -- more than the
entire window XHarness had been given -- while XHarness's own run of the same task
ended at 42,155 with a budget failure. Only one of those two arms could finish that
task, and the reason was the flag, not the harness.

**The mechanism, in the host's own words.** Re-running two of the tasks that had
ended in `error`, at the same 131,072 window, with the error text now recorded:

```
error: token budget rejected request: estimated request input (104990 tokens) exceeds
available input budget (97280); context=131072, output_reserve=32768,
minimum_output=32768, safety_margin=1024; no further safe compaction was available
(LOOP_FAILED)

error: token budget rejected request: estimated request input (97842 tokens) exceeds
available input budget (97280); context=131072, output_reserve=32768, ...
```

`cancel-async-tasks`, the third task in that group, passed on the rerun -- where the
original run had recorded `error` -- so there is run-to-run variance on top of the
budget effect, and a single trial per task does not settle a task's outcome either.

The arithmetic is stated by the message: available input budget is
`context - output_reserve - safety_margin` = 131072 - 32768 - 1024 = 97,280. One task
missed by 562 tokens, 0.6% of the budget. Under a 1,000,000-token window and the same
32,768 output reserve the budget is 967,232, and neither of those requests comes
close to it.

**A controlled rerun, changing nothing but the budget.** Three tasks that had failed
on budget in the 47x2 run, run twice: once at `context_window=1000000,
max_output_tokens=131072`, once at the `128000/32768` the main run used.

| task | 1M / 131072 | 128K / 32768 |
| --- | --- | --- |
| `circuit-fibsqrt` | **pass**, 1801 s | `max-tokens`, 487 s |
| `write-compressor` | **pass**, 274 s | `error`, 136 s |
| `adaptive-rejection-sampler` | `completed`, 1127 s | `max-tokens`, 652 s |
| | **2/3** | **0/3** |

Same tasks, same model, same machine, same harness binary. The budget decided the
outcome.

**What is still not equalised.** The output-budget semantics differ and no reachable
documentation settles them. `--max-output-tokens` is a reservation carved out of the
context window; upstream's `max_tokens` bounds a single step. Both arms are given
`1000000`/`32768`, which makes the declared window and the per-step ceiling match, so
the effective input budgets (967,232 and roughly 967,232) agree to within the safety
margin. This is measured per trial rather than assumed, and any conclusion is stated
against the measured numbers.

**The methodological point.** Both of these values were in a comment claiming they
had been "verified against the shipped binary". Neither had been. A number that
matters this much, written into an arm's configuration, is a measurement, and it
should be taken the same way as any other measurement in the bench -- from the
system, not from a note saying someone once looked.

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