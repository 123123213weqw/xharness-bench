#!/usr/bin/env python3
"""Produce the comparison numbers from run directories.

Not a report generator -- docs/results.md is hand-written, because the interesting part
of a result is the caveats and those do not come out of a table. This exists so the
numbers in it can be regenerated and checked rather than trusted, and so a task that was
rerun supersedes its earlier attempt instead of appearing twice.

    scripts/compare.py \
        --arm xharness=runs/tier-a3-xharness \
        --arm upstream=runs/tier-a2-dsh --arm upstream=runs/tier-a2-dsh-retry

An arm may be given more than once; later directories win per task, which is what makes a
retry batch meaningful. Unjudged trials are counted and reported, never scored: a trial
whose setup failed is not evidence about either harness, and treating it as a loss is how
a benchmark measures its own plumbing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xharness_bench.report import mcnemar_exact, tasks_required, wilson  # noqa: E402


def load(directory: Path) -> dict[str, dict]:
    """Every judged or unjudged trial under ``directory``, keyed by task."""
    found: dict[str, dict] = {}
    for root, _dirs, files in os.walk(directory):
        if "result.json" not in files:
            continue
        try:
            trial = json.loads((Path(root) / "result.json").read_text())
        except (OSError, ValueError):
            continue
        task = trial.get("task_name")
        if not task:
            continue
        meta = ((trial.get("agent_result") or {}).get("metadata")) or {}
        rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
        found[task] = {
            "reward": rewards.get("reward"),
            "reasons": meta.get("turn_end_reasons"),
            "elapsed": meta.get("elapsed_sec"),
            "tools": meta.get("tool_calls"),
            "version": meta.get("xharness_version") or meta.get("sdk_version"),
            "exception": (trial.get("exception_info") or {}).get("exception_type"),
        }
    return found


def summarise(name: str, arm: dict[str, dict]) -> None:
    judged = {t: v for t, v in arm.items() if v["reward"] is not None}
    passed = sum(1 for v in judged.values() if v["reward"] == 1.0)
    versions = sorted({str(v["version"]) for v in arm.values() if v["version"]})
    n = len(judged)
    if n:
        low, high = wilson(passed, n)
        rate = f"{passed}/{n} = {100 * passed / n:.1f}%  [{100 * low:.1f}, {100 * high:.1f}]"
    else:
        rate = "n/a"
    print(f"  {name:<20} {rate:<40} unjudged {len(arm) - n}")
    if versions:
        print(f"  {'':<20} version(s): {', '.join(versions)}")
    times = sorted(v["elapsed"] for v in judged.values() if isinstance(v["elapsed"], (int, float)))
    if times:
        print(f"  {'':<20} median {times[len(times) // 2]:.0f}s  max {times[-1]:.0f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="append", required=True, metavar="NAME=DIR")
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--candidate", default=None)
    args = parser.parse_args()

    arms: dict[str, dict[str, dict]] = {}
    order: list[str] = []
    for spec in args.arm:
        if "=" not in spec:
            print(f"expected NAME=DIR, got {spec!r}", file=sys.stderr)
            return 2
        name, directory = spec.split("=", 1)
        path = Path(directory)
        if not path.is_dir():
            print(f"no such directory: {path}", file=sys.stderr)
            return 2
        arms.setdefault(name, {}).update(load(path))
        if name not in order:
            order.append(name)

    print("each arm:")
    for name in order:
        summarise(name, arms[name])

    if args.baseline and args.candidate:
        base, cand = arms.get(args.baseline, {}), arms.get(args.candidate, {})
        shared = sorted(set(base) & set(cand))
        # Only pairs where both sides were actually graded. `reward != 1.0` is also true
        # for None, so the obvious version of these four lines silently scores every
        # ungraded trial as a loss -- for whichever arm failed to reach grading. The same
        # defect was fixed in report.compare() and reintroduced here within the hour,
        # which is why the pair filter is a separate expression rather than a condition
        # on each list.
        paired = [
            t for t in shared
            if base[t]["reward"] is not None and cand[t]["reward"] is not None
        ]
        unjudged = [t for t in shared if t not in paired]
        both = [t for t in paired if base[t]["reward"] == 1.0 and cand[t]["reward"] == 1.0]
        base_only = [t for t in paired if base[t]["reward"] == 1.0 and cand[t]["reward"] == 0.0]
        cand_only = [t for t in paired if base[t]["reward"] == 0.0 and cand[t]["reward"] == 1.0]
        neither = [t for t in paired if base[t]["reward"] == 0.0 and cand[t]["reward"] == 0.0]
        print()
        print(f"paired: {args.candidate} against {args.baseline}")
        print(f"  shared tasks     {len(shared)}")
        print(f"  paired (graded)  {len(paired)}  ({len(unjudged)} not graded by both)")
        print(f"  both pass        {len(both)}")
        print(f"  both fail        {len(neither)}")
        print(f"  only {args.baseline:<12} {len(base_only)}   {base_only}")
        print(f"  only {args.candidate:<12} {len(cand_only)}   {cand_only}")
        discordant = len(base_only) + len(cand_only)
        print(f"  discordant       {discordant}")
        if discordant:
            print(f"  exact McNemar p  {mcnemar_exact(len(base_only), len(cand_only)):.4f}")
        print()
        print("  resolution floor -- report this next to any conclusion:")
        for delta in (0.10, 0.05):
            print(
                f"    {delta * 100:.0f}pp difference: needs ~{tasks_required(delta)} tasks, "
                f"this set has {len(shared)}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())