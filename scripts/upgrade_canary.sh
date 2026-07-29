#!/usr/bin/env bash
# ─── The upgrade canary — ADR-016's only control that catches a stranded saga ─────────
#
# THIS SCRIPT IS THE CONTRACT. `tests/integration/test_upgrade_canary.py` implements it;
# where they differ, this file is right and the test is wrong.
#
# WHAT IT PROVES
#
#   A saga pauses for up to 30 days at the approval gate. Its state lives in a DynamoDB
#   checkpoint written by whatever version of `langgraph` + `langgraph-checkpoint-aws`
#   was deployed at pause time. If those packages are upgraded during the window, resume
#   must deserialize a checkpoint written by the OLD version.
#
#   A serialization change strands live erasure requests SILENTLY, past a statutory
#   deadline, with no error until someone asks why a subject was never erased. No unit
#   test can catch it: the failure is between two versions across a real table.
#
#   So: pause a real saga, bump BOTH pins, redeploy, resume THAT SAME THREAD from THAT
#   SAME TABLE. Anything less is a different test.
#
# WHY BOTH PINS
#
#   Serialization lives in the checkpoint package as much as in langgraph itself
#   (invariant 9). Bumping one and canarying the other is VALIDATION baseline finding #3
#   wearing new clothes — a pin protecting the wrong layer.
#
# THE CANARY_STAGE CONTRACT
#
#   The test reads $CANARY_STAGE and does exactly one of two things:
#
#     CANARY_STAGE=pause    Start a saga, drive it to the approval interrupt, and write
#                           the thread id to $CANARY_STATE. Assert it is genuinely paused
#                           — a saga that ran to completion proves nothing about resume.
#                           MUST NOT approve, and MUST NOT resume.
#
#     CANARY_STAGE=resume   Read the thread id from $CANARY_STATE. Assert the checkpoint
#                           still loads, the manifest digest is byte-identical to the one
#                           recorded at pause, and the saga resumes to completion.
#                           MUST NOT start a new saga — the point is the OLD checkpoint.
#
#   Any other value is an error. There is no default: a canary that silently picked a
#   stage would report success for whichever half happened to run.
#
# USAGE
#
#   bash scripts/upgrade_canary.sh 1.2.10 1.2.1     # target langgraph, checkpoint-aws
#   bash scripts/upgrade_canary.sh                  # no bump: proves the harness works
#
#   The no-argument form is a REHEARSAL and reports itself as one. It exercises
#   pause → redeploy → resume with the pins unchanged, which catches a broken harness
#   without claiming to have canaried an upgrade.
#
# ON FAILURE
#
#   pyproject.toml and requirements.lock are restored, and the exit is non-zero. The
#   DEPLOYED stack is left on the new versions — restoring code is cheap, redeploying is
#   not, and a human deciding what to do next needs the broken state to look at.
#   The paused thread is left paused. It is evidence.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

export CANARY_STATE="${CANARY_STATE:-$REPO/.canary-state.json}"
PYPROJECT="$REPO/pyproject.toml"
LOCKFILE="$REPO/requirements.lock"
BACKUP_DIR="$(mktemp -d)"

TARGET_LANGGRAPH="${1:-}"
TARGET_CHECKPOINT="${2:-}"

if [ -n "$TARGET_LANGGRAPH" ] && [ -z "$TARGET_CHECKPOINT" ]; then
  echo "❌ both pins move together (invariant 9). Pass two versions, or neither." >&2
  echo "   usage: bash scripts/upgrade_canary.sh <langgraph> <langgraph-checkpoint-aws>" >&2
  exit 2
fi

restore() {
  cp "$BACKUP_DIR/pyproject.toml" "$PYPROJECT"
  [ -f "$BACKUP_DIR/requirements.lock" ] && cp "$BACKUP_DIR/requirements.lock" "$LOCKFILE"
  echo "↩  pyproject.toml and requirements.lock restored to the pinned versions."
}
trap 'code=$?; if [ $code -ne 0 ]; then restore; echo "❌ upgrade canary FAILED (exit $code)" >&2; fi; rm -rf "$BACKUP_DIR"; exit $code' EXIT

cp "$PYPROJECT" "$BACKUP_DIR/pyproject.toml"
[ -f "$LOCKFILE" ] && cp "$LOCKFILE" "$BACKUP_DIR/requirements.lock"

current_pin() { grep -oE "^  \"$1==[0-9][^\"]*\"" "$PYPROJECT" | grep -oE '[0-9][^"]*'; }
FROM_LANGGRAPH="$(current_pin langgraph)"
FROM_CHECKPOINT="$(current_pin langgraph-checkpoint-aws)"

echo "── upgrade canary ────────────────────────────────────────────────"
if [ -z "$TARGET_LANGGRAPH" ]; then
  echo "   REHEARSAL — pins unchanged (langgraph $FROM_LANGGRAPH, checkpoint-aws $FROM_CHECKPOINT)."
  echo "   This proves the harness works. It does NOT canary an upgrade."
else
  echo "   langgraph              $FROM_LANGGRAPH  ->  $TARGET_LANGGRAPH"
  echo "   langgraph-checkpoint-aws  $FROM_CHECKPOINT  ->  $TARGET_CHECKPOINT"
fi
echo

# ── 1. pause, on the CURRENT versions ────────────────────────────────
echo "1. pausing a saga on the deployed versions"
rm -f "$CANARY_STATE"
CANARY_STAGE=pause .venv/Scripts/python.exe -m pytest tests/integration/test_upgrade_canary.py -q -m canary
test -s "$CANARY_STATE" || { echo "❌ the pause stage wrote no state file" >&2; exit 1; }
echo "   paused: $(cat "$CANARY_STATE")"
echo

# ── 2. bump both pins, together ──────────────────────────────────────
if [ -n "$TARGET_LANGGRAPH" ]; then
  echo "2. bumping both pins"
  # sed on the exact pinned lines; the pins are `"pkg==x.y.z"` at two-space indent, and
  # anchoring on that shape avoids rewriting the keywords list or a comment.
  sed -i.bak -E "s/^(  \"langgraph==)[0-9][^\"]*(\")/\1$TARGET_LANGGRAPH\2/" "$PYPROJECT"
  sed -i.bak -E "s/^(  \"langgraph-checkpoint-aws==)[0-9][^\"]*(\")/\1$TARGET_CHECKPOINT\2/" "$PYPROJECT"
  rm -f "$PYPROJECT.bak"
  [ "$(current_pin langgraph)" = "$TARGET_LANGGRAPH" ] || { echo "❌ the langgraph pin did not move" >&2; exit 1; }
  [ "$(current_pin langgraph-checkpoint-aws)" = "$TARGET_CHECKPOINT" ] || { echo "❌ the checkpoint pin did not move" >&2; exit 1; }
  make lock
  make install
else
  echo "2. no bump (rehearsal)"
fi
echo

# ── 3. redeploy the saga plane on the new versions ───────────────────
echo "3. redeploying — the resume must run on the NEW code against the OLD checkpoint"
make package
make deploy-dev
echo

# ── 4. resume THAT thread from THAT table ────────────────────────────
echo "4. resuming the paused thread"
CANARY_STAGE=resume .venv/Scripts/python.exe -m pytest tests/integration/test_upgrade_canary.py -q -m canary
echo

if [ -z "$TARGET_LANGGRAPH" ]; then
  echo "✅ canary REHEARSAL passed — harness works; no upgrade was tested."
else
  echo "✅ upgrade canary PASSED — langgraph $FROM_LANGGRAPH->$TARGET_LANGGRAPH, "\
       "checkpoint-aws $FROM_CHECKPOINT->$TARGET_CHECKPOINT."
  echo "   The pins in pyproject.toml are now the NEW versions. Commit them with this"
  echo "   output, or run \`git checkout pyproject.toml requirements.lock\` to abandon."
fi
