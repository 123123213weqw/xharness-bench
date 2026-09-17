"""Aggregation and inference for harness comparisons.

Two rules this module exists to enforce:

1. **Paired, not pooled.**  The same task runs under every harness, so the unit
   of analysis is the task, and harnesses are compared on their *disagreements*.
   McNemar's test on the discordant pairs is the right test; comparing two
   independent proportions throws away the pairing and inflates the variance.

2. **No point estimates without intervals.**  With a few hundred tasks the
   confidence interval on a pass rate is several points wide.  Reporting ``62%``
   without ``62% [56, 68]`` invites reading noise as signal.

Deliberately dependency-light: Wilson and the exact binomial are implemented
here so the primary numbers can be reproduced without numpy/scipy.
"""

from __future__ import annotations

import json
import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


def wilson(successes: int, total: int, z: float = 1.959963985) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because pass rates sit near 0 or 1
    in this domain, where the normal interval can extend past [0, 1].
    """
    if total == 0:
        return (0.0, 1.0)
    phat = successes / total
    denominator = 1 + z * z / total
    center = (phat + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
        / denominator
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def _log_binomial(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant counts ``b`` and ``c``.

    Exact rather than chi-square: discordant counts are usually small, which is
    exactly where the chi-square approximation is unreliable.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.exp(_log_binomial(n, i)) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


@dataclass
class Comparison:
    """Paired comparison of two harnesses over a shared task set."""

    baseline: str
    candidate: str
    both_pass: int = 0
    only_baseline: int = 0
    only_candidate: int = 0
    both_fail: int = 0
    skipped: list[str] = field(default_factory=list)
    # Tasks where both arms produced a trial but at least one had no verifier
    # verdict. Kept out of the contingency table rather than counted as a loss:
    # bool(None) is False, so the obvious implementation quietly scores every
    # unjudged trial as a failure for whichever arm failed to reach grading.
    unjudged: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.both_pass + self.only_baseline + self.only_candidate + self.both_fail

    @property
    def delta(self) -> float:
        """Candidate pass rate minus baseline pass rate, over paired tasks."""
        if self.total == 0:
            return 0.0
        return (self.only_candidate - self.only_baseline) / self.total

    def summary(self) -> dict[str, Any]:
        base_pass = self.both_pass + self.only_baseline
        cand_pass = self.both_pass + self.only_candidate
        return {
            "baseline": self.baseline,
            "candidate": self.candidate,
            "tasks": self.total,
            "baseline_pass_rate": round(base_pass / self.total, 4) if self.total else None,
            "candidate_pass_rate": round(cand_pass / self.total, 4) if self.total else None,
            "delta": round(self.delta, 4),
            "discordant": self.only_baseline + self.only_candidate,
            "p_value": round(mcnemar_exact(self.only_baseline, self.only_candidate), 4),
            "skipped": len(self.skipped),
            "unjudged": len(self.unjudged),
        }


def tasks_required(delta: float, discordance: float = 0.25, power: float = 0.8) -> int:
    """Roughly how many paired tasks are needed to detect ``delta``.

    Textbook approximation for McNemar.  Included so the design can state, up
    front, which effect sizes it is even capable of resolving -- the usual
    failure mode of these comparisons is a 30-task run presented as if it could
    detect a five-point difference.
    """
    if delta <= 0:
        return 0
    z_alpha, z_beta = 1.959963985, 0.8416212336
    pi_d = max(discordance, abs(delta) + 1e-9)
    numerator = (z_alpha * math.sqrt(pi_d) + z_beta * math.sqrt(pi_d - delta * delta)) ** 2
    return int(math.ceil(numerator / (delta * delta)))


FAILURE_KINDS = (
    "timeout",
    "budget_exhausted",
    "context_overflow",
    "declared_done_failed",
    "crash",
    "harness_error",
    "verifier_rejected",
    "no_progress",
)


def classify(row: dict[str, Any], *, verifier_untouched: bool = True) -> str:
    """Assign one mutually exclusive failure kind.

    Rule-based on purpose.  An LLM judge here would add a second stochastic
    system to a measurement whose whole point is attributing variance -- and the
    most informative bucket, ``declared_done_failed``, is exactly the one that
    is trivially decidable: the agent said it was finished and the tests
    disagree.
    """
    if not verifier_untouched:
        return "verifier_rejected"
    meta = row.get("metadata") or {}
    if row.get("passed") is None:
        # No verifier verdict at all: setup failure, timeout before grading, or a
        # harness exception. Named separately because it is not evidence about
        # either the model or the task.
        return "no_verdict"
    if row.get("passed"):
        return "passed"
    if meta.get("timeout"):
        return "timeout"
    reasons = meta.get("turn_end_reasons") or []
    if "cancelled" in reasons:
        return "crash"
    if meta.get("budget_exhausted"):
        return "budget_exhausted"
    if meta.get("context_overflow"):
        return "context_overflow"
    # A turn whose request the harness's own token budget refused never reached the
    # task. Without this the message lands in turn_end_errors and the trial is
    # classified no_progress, which reads as "the agent made no headway" for what is
    # actually "the harness would not send the request" -- the difference between a
    # finding about the model and a finding about the configuration.
    joined_errors = " ".join(meta.get("turn_end_errors") or [])
    if "token budget rejected request" in joined_errors:
        return "budget_rejected"
    if "TRANSPORT" in joined_errors:
        return "transport_error"
    if joined_errors:
        return "harness_error"
    if meta.get("final_response") and not row.get("error"):
        return "declared_done_failed"
    if row.get("error"):
        return "harness_error"
    return "no_progress"


def load_results(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in sorted(path.glob("**/result.json")):
        rows.append(json.loads(candidate.read_text(encoding="utf-8")))
    return rows


def index_by_task(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """``{task_id: {harness: row}}`` -- the shape every paired test needs."""
    indexed: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        indexed.setdefault(row["task_id"], {})[row["harness"]] = row
    return indexed


def compare(rows: Iterable[dict[str, Any]], baseline: str, candidate: str) -> Comparison:
    result = Comparison(baseline=baseline, candidate=candidate)
    for task_id, by_harness in index_by_task(rows).items():
        left, right = by_harness.get(baseline), by_harness.get(candidate)
        if left is None or right is None:
            result.skipped.append(task_id)
            continue
        if left.get("passed") is None or right.get("passed") is None:
            result.unjudged.append(task_id)
            continue
        lp, rp = bool(left.get("passed")), bool(right.get("passed"))
        if lp and rp:
            result.both_pass += 1
        elif lp:
            result.only_baseline += 1
        elif rp:
            result.only_candidate += 1
        else:
            result.both_fail += 1
    return result

# --------------------------------------------------------------------- loading
#
# Field paths below were read off real Harbor 0.23.0 output, not guessed. A
# single trial's ``result.json`` looks like:
#
#   {"task_name": "break-filter-js-from-html",
#    "task_id": {"git_url": ..., "path": "break-filter-js-from-html", ...},
#    "agent_info": {"name": "oracle", "version": "1.0.0"},
#    "agent_result": {"n_input_tokens": null, ...},
#    "verifier_result": {"rewards": {"reward": 1.0}},
#    "exception_info": null}
#
# Two traps this handles explicitly. ``rewards`` is a *dict* (tasks may score on
# several keys), not a number. And ``task_id`` is a dict, so treating it as the
# task name yields a row whose task_id is a dict -- which then silently fails to
# pair with anything. Both would have produced a confidently wrong table rather
# than an error, which is why ``--inspect`` exists.


def _first(mapping: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _dig(mapping: Any, *path: str) -> Any:
    cursor = mapping
    for step in path:
        if not isinstance(cursor, dict) or step not in cursor:
            return None
        cursor = cursor[step]
    return cursor


def _reward_of(trial: dict[str, Any]) -> float | None:
    """The scalar reward, from wherever this Harbor version puts it."""
    rewards = _dig(trial, "verifier_result", "rewards")
    if isinstance(rewards, dict):
        for key in ("reward", "score", "resolved"):
            if isinstance(rewards.get(key), (int, float)):
                return float(rewards[key])
        numbers = [v for v in rewards.values() if isinstance(v, (int, float))]
        if numbers:
            return float(numbers[0])
    for candidate in (
        trial.get("reward"),
        trial.get("score"),
        _dig(trial, "verifier_result", "reward"),
        _dig(trial, "result", "reward"),
    ):
        if isinstance(candidate, (int, float)):
            return float(candidate)
    return None


def _task_of(trial: dict[str, Any]) -> str | None:
    name = _first(trial, "task_name", "trial_name", "name")
    if isinstance(name, str):
        return name
    task_id = trial.get("task_id")
    if isinstance(task_id, str):
        return task_id
    path = _dig(task_id, "path")
    if isinstance(path, str):
        return path
    return None


def _harness_of(trial: dict[str, Any], fallback: str) -> str:
    for candidate in (
        _dig(trial, "agent_info", "name"),
        _dig(trial, "config", "agent", "name"),
        _first(trial, "agent_name", "agent", "harness"),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, dict):
            inner = _first(candidate, "name", "import_path")
            if isinstance(inner, str) and inner:
                return inner
    return fallback


def row_from_trial(
    trial: dict[str, Any], task_id: str, harness: str, source: str
) -> dict[str, Any] | None:
    """Map one Harbor trial onto this repository's flat result row.

    A missing reward is kept as a row with ``passed=None`` -- neither a pass nor a
    failure. Inferring a loss from an absent field would invent data, which is the
    one error that would corrupt a paired comparison.

    But the row is *kept*, and that matters more than it looks. Dropping it made a
    wholly failed arm print as

        parsed 28 trial rows across 1 harness(es)

    with the other 47 trials simply absent -- "no data" where the truth was
    "nothing started". An arm that never ran has to look different from an arm that
    was never measured, so unjudged trials are counted, classified as
    ``no_verdict``, and excluded from the pass-rate denominator rather than from the
    report.
    """
    reward = _reward_of(trial)

    agent_result = trial.get("agent_result") if isinstance(trial.get("agent_result"), dict) else {}
    metadata = {
        "timeout": bool(_first(trial, "timeout", default=False)),
        "final_response": _first(agent_result, "final_response", "output", default=""),
        "turn_end_reasons": _first(agent_result, "turn_end_reasons", default=[]),
        "elapsed_sec": _first(agent_result, "elapsed_sec", default=_first(trial, "elapsed_sec")),
        "tool_calls": _first(agent_result, "tool_calls"),
        "xharness_version": _first(agent_result, "xharness_version"),
        "xharness_host_sha256": _first(agent_result, "xharness_host_sha256"),
    }
    exception = trial.get("exception_info")
    metadata["exception"] = str(exception)[:400] if exception else None

    return {
        "task_id": task_id,
        "harness": harness,
        # None, not False: an unjudged trial is not a loss.
        "passed": None if reward is None else reward >= 1.0,
        "reward": reward,
        "n_input_tokens": int(_first(agent_result, "n_input_tokens", default=0) or 0),
        "n_output_tokens": int(_first(agent_result, "n_output_tokens", default=0) or 0),
        "n_cache_tokens": int(_first(agent_result, "n_cache_tokens", default=0) or 0),
        "cost_usd": _first(agent_result, "cost_usd"),
        "wall_clock_sec": _first(trial, "wall_clock_sec"),
        "metadata": {k: v for k, v in metadata.items() if v is not None},
        "error": exception,
        "source": source,
    }


def load_harbor_jobs(root: Path) -> list[dict[str, Any]]:
    """Read every Harbor trial under ``root`` into flat rows.

    Scans for ``result.json`` by name, which is where Harbor puts the trial
    record, and stays tolerant of the surrounding layout.
    """
    rows: list[dict[str, Any]] = []
    # os.walk with followlinks, not Path.glob("**/..."): glob does not descend
    # through symlinks, so a results root assembled from symlinked arm directories
    # -- the obvious way to compare two arms that Harbor wrote to separate paths --
    # matches nothing and reports zero rows. That is indistinguishable from an empty
    # run at the call site, which is the failure mode this loader was already fixed
    # for once.
    candidates: list[Path] = []
    for directory, _subdirs, filenames in os.walk(root, followlinks=True):
        if "result.json" in filenames:
            candidates.append(Path(directory) / "result.json")
    for path in sorted(candidates):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        entries: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list):
                entries = [e for e in results if isinstance(e, dict)]
            elif any(k in payload for k in ("verifier_result", "reward", "task_name")):
                entries = [payload]

        for entry in entries:
            task_id = _task_of(entry)
            if not task_id:
                continue
            row = row_from_trial(
                entry, task_id, _harness_of(entry, path.parent.name), str(path)
            )
            if row:
                rows.append(row)
    return rows


# ------------------------------------------------------------------------ CLI


def _fmt_rate(passed: int, total: int) -> str:
    if total == 0:
        return "n/a"
    low, high = wilson(passed, total)
    return f"{passed / total * 100:5.1f}% [{low * 100:4.1f},{high * 100:4.1f}]"


def render(
    rows: list[dict[str, Any]], baseline: str | None, candidate: str | None
) -> str:
    lines: list[str] = []
    by_harness: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_harness.setdefault(row["harness"], []).append(row)

    if not by_harness:
        return (
            "no results parsed -- check --results, or use --inspect to see what "
            "was actually found"
        )

    lines.append(f"parsed {len(rows)} trial rows across {len(by_harness)} harness(es)")
    lines.append("")
    lines.append(f"{'harness':<24}{'pass rate [95% CI]':<28}{'n':>4}  {'med s':>7}")
    lines.append("-" * 72)
    for name in sorted(by_harness):
        group = by_harness[name]
        judged = [r for r in group if r["passed"] is not None]
        passed = sum(1 for r in judged if r["passed"])
        elapsed = sorted(
            r["metadata"].get("elapsed_sec")
            for r in judged
            if isinstance(r["metadata"].get("elapsed_sec"), (int, float))
        )
        median = f"{elapsed[len(elapsed) // 2]:.0f}" if elapsed else "-"
        rate = _fmt_rate(passed, len(judged))
        if len(judged) != len(group):
            # Make the shortfall unmissable next to the rate it is missing from.
            rate += f" ({len(group) - len(judged)} unjudged)"
        lines.append(f"{name:<24}{rate:<28}{len(group):>4}  {median:>7}")

    lines.append("")
    lines.append("failure classification (rule-based, mutually exclusive)")
    lines.append("-" * 72)
    for name in sorted(by_harness):
        kinds: dict[str, int] = {}
        for row in by_harness[name]:
            kind = classify(row)
            kinds[kind] = kinds.get(kind, 0) + 1
        rendered = "  ".join(
            f"{k}={v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"{name:<24}{rendered}")

    if baseline and candidate and baseline in by_harness and candidate in by_harness:
        comparison = compare(rows, baseline, candidate)
        summary = comparison.summary()
        lines.append("")
        lines.append(f"paired comparison: {candidate} vs {baseline}")
        lines.append("-" * 72)
        lines.append(f"  paired tasks      {summary['tasks']}")
        lines.append(f"  {baseline:<17} {summary['baseline_pass_rate']}")
        lines.append(f"  {candidate:<17} {summary['candidate_pass_rate']}")
        lines.append(f"  delta             {summary['delta']:+.4f}")
        lines.append(
            f"  discordant        {summary['discordant']} "
            f"({comparison.only_baseline} only-{baseline}, "
            f"{comparison.only_candidate} only-{candidate})"
        )
        lines.append(f"  exact McNemar p   {summary['p_value']}")
        if summary["unjudged"]:
            lines.append(
                f"  unjudged pairs    {summary['unjudged']} "
                "(a trial existed but no verifier verdict: excluded, not scored)"
            )
        if summary["skipped"]:
            lines.append(f"  skipped unpaired  {summary['skipped']}")
        lines.append("")
        lines.append("  resolution floor (report this next to any conclusion):")
        for target in (0.10, 0.05):
            needed = tasks_required(target)
            verdict = (
                "resolvable"
                if summary["tasks"] >= needed
                else f"UNDERPOWERED, needs ~{needed}"
            )
            lines.append(f"    {int(target * 100):>2}pp difference: {verdict}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Aggregate harness comparison results")
    parser.add_argument("--results", type=Path, default=Path("runs"))
    parser.add_argument("--baseline")
    parser.add_argument("--candidate")
    parser.add_argument("--json", action="store_true", help="emit parsed rows as JSON")
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="print the first parsed rows verbatim, to diagnose schema drift",
    )
    args = parser.parse_args(argv)

    rows = load_harbor_jobs(args.results)

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0 if rows else 1
    if args.inspect:
        print(f"{len(rows)} row(s) parsed from {args.results}")
        for row in rows[:5]:
            print(json.dumps(row, indent=2, ensure_ascii=False))
        return 0

    print(render(rows, args.baseline, args.candidate))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())