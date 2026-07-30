"""`.github/workflows/ci.yml` is a gate, so its shape is asserted rather than trusted.

CI is the only place several controls actually run — the ephemeral per-PR eval stack, the
`always()` teardown, the upgrade canary. Until now nothing checked it, and a workflow is
the worst place to rely on review: a shell typo or an inverted condition surfaces on a
push, in a job that costs money, and a *vacuous* one surfaces never.

Two defects motivated this file, both found while opening M10 and both invisible to
reading the YAML alone:

* **V13-1** — `scripts/upgrade_canary.sh` hardcoded `.venv/Scripts/python.exe`. The
  `upgrade-canary` job runs on `ubuntu-latest`, where that path does not exist, so the job
  would have died on its first line. It had never run.
* **V13-2** — the same job invoked the canary with **no arguments**, which is the
  rehearsal form. It fires *because* the pins changed, so its checkout already holds the
  new versions: it paused and resumed on the same version and reported success for a
  transition it never tested. Green and vacuous.

The `bash -n` harness here self-tests in both directions before it is trusted. An earlier
version of it passed CRLF-terminated scripts to bash and reported a syntax error in valid
YAML — a checker that cannot fail correctly reports success for anything, which is the
shape `docs/VALIDATION.md` has recorded six times.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CANARY_SCRIPT = REPO / "scripts" / "upgrade_canary.sh"

#: GitHub expressions are not shell. Substituted with a literal so `bash -n` sees a
#: well-formed word rather than `${{ ... }}`.
_EXPRESSION = re.compile(r"\$\{\{[^}]*\}\}")


def _parse(script: str) -> tuple[int, str]:
    """`bash -n` on a script, fed as BYTES with `\\n` endings.

    `text=True` would translate to CRLF on Windows, and `then\\r` is not `then` — the
    harness would then reject valid multi-line scripts while passing single-line ones,
    because those have no newline to mangle.
    """
    payload = ("set -euo pipefail\n" + script).replace("\r\n", "\n").encode("utf-8")
    result = subprocess.run(["bash", "-n"], input=payload, capture_output=True)
    return result.returncode, result.stderr.decode("utf-8", "replace")


def _workflow() -> dict[str, Any]:
    return dict(yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _steps(job: str) -> list[dict[str, Any]]:
    return list(_workflow()["jobs"][job]["steps"])


def _step_index(job: str, needle: str) -> int:
    for index, step in enumerate(_steps(job)):
        if needle in str(step.get("name", "")) or needle in str(step.get("run", "")):
            return index
    raise AssertionError(f"no step in {job} matching {needle!r}")


def test_the_bash_harness_can_both_fail_and_pass() -> None:
    """Before trusting any verdict below. A checker that cannot go red is decoration."""
    broken, _ = _parse("if [ -z ; then\n")
    assert broken != 0, "the harness did not reject a syntax error"
    valid, error = _parse('if [ "a" = "b" ]; then\n  echo x\nelif [ -n "c" ]; then\n  echo y\nfi\n')
    assert valid == 0, f"the harness rejected a VALID multi-line script: {error}"


@pytest.mark.parametrize("job", sorted(_workflow()["jobs"]))
def test_every_run_step_is_valid_shell(job: str) -> None:
    """A shell typo in a workflow surfaces on a push, in a job that spends money."""
    for index, step in enumerate(_steps(job)):
        script = step.get("run")
        if not script:
            continue
        code, error = _parse(_EXPRESSION.sub("PLACEHOLDER", str(script)))
        assert code == 0, f"{job} step {step.get('name', index)!r} is not valid shell:\n{error}"


def test_the_canary_job_passes_its_target_versions() -> None:
    """V13-2. The job fires because the pins changed, so its checkout already holds the
    NEW versions — the no-argument form pauses and resumes on the same one."""
    step = _steps("upgrade-canary")[_step_index("upgrade-canary", "make upgrade-canary")]
    run = str(step["run"])
    rehearsal = (
        "the canary is invoked without both target versions — that is the REHEARSAL form, "
        "which proves the harness works and nothing about the upgrade that triggered the job"
    )
    assert "LANGGRAPH=" in run, rehearsal
    assert "CHECKPOINT_AWS=" in run, rehearsal


def test_the_canary_job_rolls_back_to_the_base_pins_before_installing() -> None:
    """Order is the gate. The pause must be checkpointed by the OLD versions, so the
    rollback has to precede both the install and the deploy — installing first would put
    the PR's versions in the venv and the "old" checkpoint would be a new one."""
    rollback = _step_index("upgrade-canary", "Roll the pins back")
    install = _step_index("upgrade-canary", "make install")
    deploy = _step_index("upgrade-canary", "make deploy-dev")
    assert rollback < install < deploy, (
        f"rollback={rollback} install={install} deploy={deploy} — the canary would pause "
        f"on the versions it is supposed to be upgrading FROM only if rollback comes first"
    )


def test_the_rollback_covers_every_file_that_pins_a_version() -> None:
    """All three pin locations must start at base. Rolling back only pyproject would leave
    SAGA_PINS ahead, and `make check` inside the canary would fail on
    test_the_makefile_ships_exactly_the_pyproject_pins — a green-to-red flip caused by the
    workflow rather than by the upgrade (V12-6's neighbour)."""
    step = _steps("upgrade-canary")[_step_index("upgrade-canary", "Roll the pins back")]
    run = str(step["run"])
    for name in ("pyproject.toml", "Makefile", "requirements.lock"):
        assert name in run, f"the rollback leaves {name} at the PR's version"


@pytest.mark.parametrize("job", ["deployed", "upgrade-canary"])
def test_teardown_runs_on_always(job: str) -> None:
    """A failed run must not leak resources. Since ADR-021 nothing bills a floor, but a
    leaked Object Lock bucket cannot be deleted by anyone until retention expires."""
    destroy = [s for s in _steps(job) if "make destroy-dev" in str(s.get("run", ""))]
    assert destroy, f"{job} never tears its stack down"
    for step in destroy:
        assert "always()" in str(step.get("if", "")), (
            f"{job}'s teardown is conditional on success — the runs that leak are the failing ones"
        )


def test_the_canary_script_is_not_platform_specific() -> None:
    """V13-1. The script is authored on Windows and executed by CI on Linux."""
    source = CANARY_SCRIPT.read_text(encoding="utf-8")
    assert "$VENV_PY" in source, "the script no longer resolves the interpreter through VENV_PY"

    # Only lines that RUN something matter. The platform-detection block mentions both
    # layouts by necessity, so counting occurrences would break the moment a third
    # legitimate mention appeared — the test would then be measuring the wrong thing.
    invocations = [
        line.strip()
        for line in source.splitlines()
        if not line.lstrip().startswith("#") and re.search(r"\.venv/(Scripts|bin)\S*\s+-m\b", line)
    ]
    assert not invocations, (
        f"these lines invoke a platform-specific interpreter directly: {invocations}. The "
        f"upgrade-canary job runs on ubuntu-latest, where .venv/Scripts does not exist — "
        f'use "$VENV_PY", which the script resolves for both layouts.'
    )
