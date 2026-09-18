#!/usr/bin/env bash
# Capture the two real-session fixtures the smoke test asserts against.
#
# Why this exists: `tests/smoke_local.py` is not allowed to invent the event shape it
# parses. The first version did exactly that -- it built `{"call": {"name": ...}}` by
# hand, the shape the Rust enum implies, and passed -- while every tool name came back
# empty on both real arms, whose events are flat camelCase. A test written from the same
# assumption as the code certifies the assumption. See docs/validity.md T22.
#
# So the test reads two captured artefacts instead:
#
#   real-session-xharness.json   the wire projection, as the arm's own trace exporter
#                                wrote it into /logs/artifacts during a trial
#   real-session-durable.json    the durable session log, whose tool events nest one
#                                level deeper (`data.call` / `data.result`)
#
# Both are produced by the arm, not by this repository, and the expected numbers in the
# test are read off them -- so regenerating from a different session changes those
# numbers, and the diff says so.
#
# This must run on the reference host, where the trials and the host state live. The
# fixtures are small (roughly 100 KB and 30 KB) and belong in the repository; until they
# are committed, a clean clone fails both shape checks by name rather than passing
# without measuring anything.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

WIRE="${WIRE:-}"       # default: newest trial that exported a trace
DURABLE="${DURABLE:-}" # default: newest session in the host state directory
OUT="${OUT:-tests/fixtures}"

if [ -z "$WIRE" ]; then
  WIRE="$(ls -t runs/ver-trace/*/*/artifacts/logs/artifacts/xharness/events.json 2>/dev/null | head -1)"
fi
if [ -z "$DURABLE" ]; then
  DURABLE="$(ls -t "$HOME/xharness/state"/*/*.jsonl "$HOME/xharness/state"/*.jsonl 2>/dev/null | head -1)"
fi

for f in "$WIRE" "$DURABLE"; do
  if [ -z "$f" ] || [ ! -f "$f" ]; then
    echo "capture-fixtures: no input found (WIRE=$WIRE DURABLE=$DURABLE)" >&2
    echo "  pass WIRE=/path/events.json DURABLE=/path/session.jsonl explicitly" >&2
    exit 2
  fi
done

mkdir -p "$OUT"
cp "$WIRE" "$OUT/real-session-xharness.json"

"$HOME/bench-venv/bin/python" - "$DURABLE" "$OUT/real-session-durable.json" <<'PY'
import json, sys
from collections import Counter

source, target = sys.argv[1], sys.argv[2]
out = []
for line in open(source, encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line:
        continue
    record = json.loads(line)
    if record.get("record") != "batch":
        continue
    for item in (record.get("events") or []):
        event = item.get("event", item)
        if isinstance(event, dict):
            out.append(event)
    # Four calls and their results is enough to pin both shapes and the identity link.
    if len([e for e in out if e.get("type") == "tool/result"]) >= 4:
        break

with open(target, "w", encoding="utf-8") as handle:
    json.dump(out, handle, ensure_ascii=False, indent=1)

print(f"  {target}: {len(out)} events {Counter(e.get('type') for e in out).most_common(6)}")
PY

echo
echo "Both fixtures captured under $OUT. Now re-run the suite and check that the"
echo "numbers it expects still appear in tests/smoke_local.py -- if the capture came"
echo "from a different session they will differ, and the expectations have to be"
echo "read off the new artefact rather than adjusted to make the test pass."
