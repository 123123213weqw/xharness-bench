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