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
#   **A target equal to the current pin is reported as "already current", not as an
#   upgrade.** The two packages release independently, so the honest case where one has
#   a newer version and the other does not is normal — and passing the current version
#   for the second is how invariant 9's "both move together" is satisfied without
#   inventing a release. What must never happen is a summary claiming both were canaried
#   when one never moved.
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

moved() { [ -n "$2" ] && [ "$1" != "$2" ]; }
MOVED_ANY=no
if moved "$FROM_LANGGRAPH" "$TARGET_LANGGRAPH" || moved "$FROM_CHECKPOINT" "$TARGET_CHECKPOINT"; then
  MOVED_ANY=yes
fi

describe_pin() { # name from to
  if [ -z "$3" ]; then printf '   %-26s %s (unchanged)\n' "$1" "$2"
  elif [ "$2" = "$3" ]; then printf '   %-26s %s (already current — nothing to canary)\n' "$1" "$2"
  else printf '   %-26s %s  ->  %s\n' "$1" "$2" "$3"
  fi
}

echo "── upgrade canary ────────────────────────────────────────────────"
describe_pin langgraph "$FROM_LANGGRAPH" "$TARGET_LANGGRAPH"
describe_pin langgraph-checkpoint-aws "$FROM_CHECKPOINT" "$TARGET_CHECKPOINT"
if [ "$MOVED_ANY" = no ]; then
  echo
  echo "   REHEARSAL — no pin moves. This proves the harness works end to end."
  echo "   It does NOT canary an upgrade, and the summary will say so."
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
  # Hermetic first, and before the deploy. A new version that breaks an API we call
  # would otherwise surface as a mysterious resume failure four minutes later, after a
  # deploy paid for; `make check` turns that into three named unit failures for free.
  # The pause has already happened, so the paused thread is still there to resume from
  # once the pins are restored.
  echo "   running the hermetic gate on the new versions before spending a deploy"
  make check
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

if [ "$MOVED_ANY" = no ]; then
  echo "✅ canary REHEARSAL passed — harness works; NO upgrade was tested."
  if [ -n "$TARGET_LANGGRAPH" ]; then
    echo "   Both targets equalled the current pins. Nothing about a version change was"
    echo "   proved, and this run must not be cited as a canary for one."
  fi
else
  echo "✅ upgrade canary PASSED — a saga paused on the old versions resumed on the new"
  echo "   ones, from the same DynamoDB table, with a byte-identical manifest digest."
  describe_pin langgraph "$FROM_LANGGRAPH" "$TARGET_LANGGRAPH"
  describe_pin langgraph-checkpoint-aws "$FROM_CHECKPOINT" "$TARGET_CHECKPOINT"
  echo
  echo "   A pin marked 'already current' was NOT canaried — there was no newer release"
  echo "   to canary. When one appears, it needs its own run (invariant 9)."
  echo "   The pins in pyproject.toml are now the targets. Commit them with this output,"
  echo "   or run \`git checkout pyproject.toml requirements.lock\` to abandon."
fi
