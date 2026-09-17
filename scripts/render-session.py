#!/usr/bin/env python3
"""Render a durable session log into a readable trace.

The host writes every session to a JSONL file: one header record, then batch records
carrying event arrays. That file is the complete, ordered account of a run -- every
prompt, every assistant message, every tool call with its arguments, every result with
the bytes it returned, and why the turn ended.

The adapter exports a trace at the end of a trial, which is what you want during a run.
This script is for the other case: a session log that already exists. Live containers
carry one at ``/opt/xharness/state/sessions/bench-trial.jsonl`` and can be copied out
with ``docker cp``, so a trial still in flight can be read without waiting for it, and a
log kept from an earlier run can be re-read without re-running anything.

    scripts/render-session.py session.jsonl                  # to stdout
    scripts/render-session.py session.jsonl -o trace.txt
    docker cp <container>:/opt/xharness/state/sessions/bench-trial.jsonl .
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xharness_bench.trace import render_trace  # noqa: E402


def load_session(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Flatten a JSONL session log into (header, events).

    Batches are the reason this is not a one-line json.load: events arrive inside
    ``record: batch`` envelopes, several per line, and reading the lines as events
    yields nothing but the envelope keys.
    """
    header: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("record")
            if kind == "header":
                header = record.get("header") or {}
            elif kind == "batch":
                for entry in record.get("events") or []:
                    if not isinstance(entry, dict):
                        continue
                    event = entry.get("event", entry)
                    if isinstance(event, dict) and isinstance(event.get("type"), str):
                        events.append(event)
    return header, events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="path to a session .jsonl")
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    if not args.session.exists():
        print(f"no such session log: {args.session}", file=sys.stderr)
        return 2

    header, events = load_session(args.session)
    if not events:
        print(f"{args.session} held no events", file=sys.stderr)
        return 1

    meta = {
        "session": header.get("id"),
        "cwd": header.get("cwd"),
        "source": str(args.session),
    }
    rendered = render_trace(events, meta=meta, label=args.label or args.session.name)

    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"{len(events)} events -> {args.output} ({len(rendered)} bytes)")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())