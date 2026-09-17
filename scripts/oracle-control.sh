#!/usr/bin/env bash
# Run the oracle over the frozen task set under the comparison's own conditions.
#
# Why this exists: the gate that validated the task set used the oracle, and the
# oracle does nothing but run the reference solution -- so the machine is nearly
# idle while the verifier runs. The comparison puts two LLM agents on the same box
# for minutes at a time, which is a different load profile, and 5 of the 47 frozen
# tasks assert on wall-clock time. `largest-eigenval` is the clearest case:
#
#     assert dt < ref_dt, f"{dt:.6f} seconds/call > {ref_dt:.6f} seconds/call"
#
# two medians compared with no margin, over matrices small enough that both sides
# are microseconds. On an idle host that passes; under two concurrent agents it
# measured 12 to 25 microseconds and failed.
#
# So "the oracle passes" is not sufficient evidence that a task can register a
# harness difference under the conditions the comparison actually runs in. This
# script measures the second thing.
#
# It costs no tokens, and it must run on a quiet host -- the whole point is to
# reproduce the comparison's load, not to add to it.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

CONCURRENCY="${CONCURRENCY:-2}"
OUT="${OUT:-runs/oracle-control}"

MOUNTS='[{"type":"bind","source":"'"$HOME"'/uvwarm/cache","target":"/root/.cache/uv"},{"type":"bind","source":"'"$HOME"'/uvwarm/python","target":"/root/.local/share/uv/python","read_only":true}]'

echo "oracle control: frozen task set, -n $CONCURRENCY, cache mounted, out=$OUT"
scripts/quiet-gate.sh "$HOME/bench-venv/bin/harbor" run \
  -p "$HOME/xh-tier-a" \
  --agent oracle \
  --mounts "$MOUNTS" \
  --agent-timeout-multiplier 4 \
  --agent-setup-timeout-multiplier 4 \
  --verifier-timeout-multiplier 2 \
  -n "$CONCURRENCY" -y \
  -o "$OUT"

echo
echo "Any task the oracle fails here is a task where the comparison cannot"
echo "attribute a difference to the harness. Compare against the frozen set in"
echo "frozen/tier-a-plan.json before reading the arm results."