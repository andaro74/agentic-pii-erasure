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
# WHERE THE PINS LIVE — TWO PACKAGES, THREE FILES
#
#   pyproject.toml (what the tests run against) · requirements.lock (generated from it)
#   · the Makefile's SAGA_PINS (what `make package` installs into the saga Lambda).
#   Bumping two of the three made this script unable to pass at all (V12-6), and the
#   contract test now derives that file list from the tree rather than restating it.
#
# ON FAILURE
#
#   pyproject.toml, the Makefile and requirements.lock are restored, and the exit is
#   non-zero. The DEPLOYED stack is left on the new versions — restoring code is cheap,
#   redeploying is not, and a human deciding what to do next needs the broken state to
#   look at. The paused thread is left paused. It is evidence.
#
#   The venv is left on the new versions too, and `restore()` says so — but only when
#   `make install` actually ran (V12-7b).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# The venv layout differs by platform: Windows puts interpreters in .venv/Scripts, POSIX
# in .venv/bin. The Makefile has auto-detected this since M0; this script hardcoded
# `.venv/Scripts/python.exe`, so the `upgrade-canary` job in .github/workflows/ci.yml —
# which runs on ubuntu-latest — would have failed on its first line every time (V13-1).
# It has never run, which is the only reason nobody found out.
if [ -x "$REPO/.venv/Scripts/python.exe" ]; then
  VENV_PY="$REPO/.venv/Scripts/python.exe"
elif [ -x "$REPO/.venv/bin/python" ]; then
  VENV_PY="$REPO/.venv/bin/python"
else
  echo "❌ no venv interpreter under .venv — run \`make install\` first." >&2
  exit 2
fi

export CANARY_STATE="${CANARY_STATE:-$REPO/.canary-state.json}"
PYPROJECT="$REPO/pyproject.toml"
LOCKFILE="$REPO/requirements.lock"
#: The THIRD place a pin lives. `SAGA_PINS` is what `make package` installs into the
#: Lambda asset, and it is a separate string on purpose so a reader can see what ships
#: (`tests/unit/test_saga_pins.py` compares it against pyproject verbatim). A canary that
#: bumped two of the three could never pass — V12-6, caught by the `make check` this
#: script now runs before spending a deploy.
MAKEFILE="$REPO/Makefile"
BACKUP_DIR="$(mktemp -d)"

#: Set the moment `make install` puts the target versions into the venv, so `restore()`
#: only warns about an environment it actually changed.
INSTALLED_TARGETS=no

TARGET_LANGGRAPH="${1:-}"
TARGET_CHECKPOINT="${2:-}"

if [ -n "$TARGET_LANGGRAPH" ] && [ -z "$TARGET_CHECKPOINT" ]; then
  echo "❌ both pins move together (invariant 9). Pass two versions, or neither." >&2
  echo "   usage: bash scripts/upgrade_canary.sh <langgraph> <langgraph-checkpoint-aws>" >&2
  exit 2
fi

restore() {
  cp "$BACKUP_DIR/pyproject.toml" "$PYPROJECT"
  cp "$BACKUP_DIR/Makefile" "$MAKEFILE"
  [ -f "$BACKUP_DIR/requirements.lock" ] && cp "$BACKUP_DIR/requirements.lock" "$LOCKFILE"
  echo "↩  pyproject.toml, Makefile and requirements.lock restored to the pinned versions."
  # The venv is NOT rolled back, on purpose: a failure is worth inspecting on the versions
  # that produced it. But that leaves the environment disagreeing with the files, and
  # `make check` fails on `test_the_installed_versions_match_the_pins` until it is fixed —
  # correctly, since a checkpoint-shaped test result from the wrong version is worthless.
  # Saying so here is the difference between a deliberate state and a confusing one.
  #
  # Guarded, because `make lock` runs first and can fail before the venv is ever touched.
  # An unconditional warning sent an operator to reinstall an environment nothing had
  # changed, which is how a true warning stops being read (V12-7b).
  if [ "$INSTALLED_TARGETS" = yes ]; then
    echo "⚠  the venv still has the TARGET versions installed. That is deliberate — inspect"
    echo "   the failure on the versions that caused it. \`make check\` will fail on"
    echo "   test_the_installed_versions_match_the_pins until you run:  make install"
  else
    echo "   The venv was never modified — the run failed before \`make install\`."
  fi
}
trap 'code=$?; if [ $code -ne 0 ]; then restore; echo "❌ upgrade canary FAILED (exit $code)" >&2; fi; rm -rf "$BACKUP_DIR"; exit $code' EXIT

cp "$PYPROJECT" "$BACKUP_DIR/pyproject.toml"
cp "$MAKEFILE" "$BACKUP_DIR/Makefile"
[ -f "$LOCKFILE" ] && cp "$LOCKFILE" "$BACKUP_DIR/requirements.lock"

current_pin() { grep -oE "^  \"$1==[0-9][^\"]*\"" "$PYPROJECT" | grep -oE '[0-9][^"]*'; }
shipped_pin() { grep -oE "\"$1==[0-9][^\"]*\"" "$MAKEFILE" | head -1 | grep -oE '[0-9][^"]*'; }
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
CANARY_STAGE=pause "$VENV_PY" -m pytest tests/integration/test_upgrade_canary.py -q -m canary
test -s "$CANARY_STATE" || { echo "❌ the pause stage wrote no state file" >&2; exit 1; }
echo "   paused: $(cat "$CANARY_STATE")"
echo

# ── 2. bump both pins, in all three places ───────────────────────────
if [ -n "$TARGET_LANGGRAPH" ]; then
  echo "2. bumping both pins, in pyproject.toml AND the Makefile's SAGA_PINS"
  # sed on the exact pinned lines; the pins are `"pkg==x.y.z"` at two-space indent, and
  # anchoring on that shape avoids rewriting the keywords list or a comment.
  sed -i.bak -E "s/^(  \"langgraph==)[0-9][^\"]*(\")/\1$TARGET_LANGGRAPH\2/" "$PYPROJECT"
  sed -i.bak -E "s/^(  \"langgraph-checkpoint-aws==)[0-9][^\"]*(\")/\1$TARGET_CHECKPOINT\2/" "$PYPROJECT"
  rm -f "$PYPROJECT.bak"
  # SAGA_PINS is what `make package` installs into the Lambda asset. Leaving it behind
  # does not ship the old version quietly — `make lock` has just written the new one into
  # requirements.lock, which `make package` passes as a constraint, so pip becomes
  # unresolvable at step 3. Either way the canary cannot pass, which is why it never had
  # (V12-6). The leading quote is what keeps the `langgraph` pattern off
  # `langgraph-checkpoint-aws`.
  sed -i.bak -E "s/(\"langgraph==)[0-9][^\"]*(\")/\1$TARGET_LANGGRAPH\2/" "$MAKEFILE"
  sed -i.bak -E "s/(\"langgraph-checkpoint-aws==)[0-9][^\"]*(\")/\1$TARGET_CHECKPOINT\2/" "$MAKEFILE"
  rm -f "$MAKEFILE.bak"
  [ "$(current_pin langgraph)" = "$TARGET_LANGGRAPH" ] || { echo "❌ the langgraph pin did not move" >&2; exit 1; }
  [ "$(current_pin langgraph-checkpoint-aws)" = "$TARGET_CHECKPOINT" ] || { echo "❌ the checkpoint pin did not move" >&2; exit 1; }
  [ "$(shipped_pin langgraph)" = "$TARGET_LANGGRAPH" ] || { echo "❌ SAGA_PINS still ships the old langgraph — the Lambda would not run what was canaried" >&2; exit 1; }
  [ "$(shipped_pin langgraph-checkpoint-aws)" = "$TARGET_CHECKPOINT" ] || { echo "❌ SAGA_PINS still ships the old checkpoint-aws" >&2; exit 1; }
  make lock
  make install
  INSTALLED_TARGETS=yes
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
CANARY_STAGE=resume "$VENV_PY" -m pytest tests/integration/test_upgrade_canary.py -q -m canary
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
  echo "   The pins in pyproject.toml, the Makefile and requirements.lock are now the"
  echo "   targets. Commit all three with this output, or abandon with:"
  echo "     git checkout pyproject.toml Makefile requirements.lock"
fi
