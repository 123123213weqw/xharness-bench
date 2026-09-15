"""Freeze the task list and the run order before anything is measured.

Countermeasure T10 in ``docs/validity.md``: a comparison is only trustworthy if
the set of tasks was fixed *before* the results were seen. Otherwise the
temptation to drop an inconvenient task is invisible in the final table -- the
table just quietly contains fewer rows.

Two things are frozen:

1. **The task list**, by ID and by a digest over the ID set. Published before the
   run; any later deviation is detectable.
2. **The execution order**, interleaved by task rather than blocked by arm.

The second one is countermeasure T2. Running all of arm A and then all of arm B
means that any drift over the run -- provider latency, a silent model update, a
rate-limit episode, a container host getting warm -- is attributed to the
difference between arms. Interleaving turns that drift into noise within each
task instead of a bias between arms.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence


def task_set_digest(task_ids: Iterable[str]) -> str:
    """Digest over an order-independent set of task IDs.

    Sorted and newline-joined, so the digest depends on *which* tasks are in the
    run and not on the order they happened to be listed in.
    """
    unique = sorted({str(task_id).strip() for task_id in task_ids if str(task_id).strip()})
    payload = "\n".join(unique).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class FrozenPlan:
    """A frozen task list plus the interleaved schedule derived from it."""

    dataset: str
    dataset_version: str
    task_ids: list[str]
    arms: list[str]
    repeats: int = 1
    order: str = "interleaved"
    schedule: list[tuple[str, str]] = field(default_factory=list)

    @property
    def digest(self) -> str:
        return task_set_digest(self.task_ids)

    def build_schedule(self) -> list[tuple[str, str]]:
        """``[(task_id, arm)]`` in execution order.

        Interleaved: for every task, every arm, back to back. That keeps the two
        arms of one task adjacent in time, which is the strongest available
        control on drift, and it also means a container or credential failure
        hits both arms of a task rather than only the second arm.
        """
        schedule: list[tuple[str, str]] = []
        for _ in range(max(1, self.repeats)):
            for task_id in self.task_ids:
                for arm in self.arms:
                    schedule.append((task_id, arm))
        self.schedule = schedule
        return schedule

    def to_json(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "task_count": len(self.task_ids),
            "task_set_sha256": self.digest,
            "task_ids": self.task_ids,
            "arms": self.arms,
            "repeats": self.repeats,
            "order": self.order,
            "schedule_length": len(self.schedule),
        }


def freeze(
    *,
    dataset: str,
    dataset_version: str,
    task_ids: Sequence[str],
    arms: Sequence[str],
    repeats: int = 1,
    output: Path | None = None,
) -> FrozenPlan:
    """Build the plan and (optionally) write it out as the record of intent."""
    plan = FrozenPlan(
        dataset=dataset,
        dataset_version=dataset_version,
        task_ids=sorted({str(t) for t in task_ids}),
        arms=list(arms),
        repeats=repeats,
    )
    plan.build_schedule()
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(plan.to_json(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return plan


def verify(frozen_path: Path, observed_task_ids: Iterable[str]) -> tuple[bool, str]:
    """Check an observed task set against a previously frozen plan.

    Returns ``(ok, message)``. A mismatch means the run did not measure the thing
    that was announced, which must be reported rather than quietly accepted.
    """
    record = json.loads(frozen_path.read_text(encoding="utf-8"))
    expected = record.get("task_set_sha256")
    observed = task_set_digest(observed_task_ids)
    if expected == observed:
        return True, f"task set matches the frozen plan ({record.get('task_count')} tasks)"

    frozen_ids = set(record.get("task_ids", []))
    seen = {str(t).strip() for t in observed_task_ids if str(t).strip()}
    missing = sorted(frozen_ids - seen)
    extra = sorted(seen - frozen_ids)
    details = []
    if missing:
        details.append(f"{len(missing)} missing (e.g. {missing[:3]})")
    if extra:
        details.append(f"{len(extra)} unexpected (e.g. {extra[:3]})")
    return False, (
        f"task set does NOT match the frozen plan; expected {expected[:12]}..., "
        f"observed {observed[:12]}...; " + "; ".join(details)
    )


def resolution_floor_note(task_count: int, delta: float = 0.05) -> str:
    """State, up front, what this run size can and cannot resolve."""
    from .report import tasks_required

    needed = tasks_required(delta)
    if task_count >= needed:
        return (
            f"{task_count} tasks is enough to resolve a {delta:.0%} difference "
            f"(needs ~{needed})"
        )
    return (
        f"{task_count} tasks CANNOT resolve a {delta:.0%} difference "
        f"(needs ~{needed}); only larger effects are measurable"
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Freeze a task list before running")
    parser.add_argument("--dataset", required=True, help="e.g. terminal-bench")
    parser.add_argument("--version", required=True, help="e.g. 2.0")
    parser.add_argument(
        "--tasks",
        required=True,
        type=Path,
        help="file with one task ID per line (e.g. the dataset's task listing)",
    )
    parser.add_argument("--arms", required=True, help="comma-separated arm names")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("frozen-plan.json"))
    parser.add_argument(
        "--verify",
        type=Path,
        help="instead of freezing, verify an observed listing against a frozen plan",
    )
    args = parser.parse_args(argv)

    task_ids = [
        line.strip()
        for line in args.tasks.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    if args.verify:
        ok, message = verify(args.verify, task_ids)
        print(("OK: " if ok else "MISMATCH: ") + message)
        return 0 if ok else 1

    plan = freeze(
        dataset=args.dataset,
        dataset_version=args.version,
        task_ids=task_ids,
        arms=[a.strip() for a in args.arms.split(",") if a.strip()],
        repeats=args.repeats,
        output=args.output,
    )
    print(f"frozen {len(plan.task_ids)} tasks for {plan.dataset}@{plan.dataset_version}")
    print(f"  task set sha256 : {plan.digest}")
    print(f"  arms            : {', '.join(plan.arms)}")
    print(f"  schedule        : {len(plan.schedule)} trials ({plan.order}, repeats={plan.repeats})")
    print(f"  written to      : {args.output}")
    print(f"  resolution      : {resolution_floor_note(len(plan.task_ids))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())