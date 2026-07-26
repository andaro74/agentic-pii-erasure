"""The AWS-touching make targets must load `.env` — and let the shell win.

Two defects live here, both of the class docs/VALIDATION.md exists to catch:

* **A setting with no mechanism.** `.env.example` documents `AWS_REGION`, but make
  does not read `.env`. Before the targets sourced it explicitly, a user could set
  a region there and deploy to whatever their AWS profile said — a regional stack
  in a region they did not choose, failing at discovery time rather than at deploy.
* **A shared "ephemeral" stack (V3-1, in new clothes).** `make install` writes a
  `.env` from the example, so a target that sourced it *unconditionally* would
  override the `PII_ERASURE_STAGE=pr-<run_id>` CI passes as job env — putting every
  concurrent PR back on one stack that they take turns destroying.

So the rule is: `.env` fills gaps, an exported shell variable always wins. These
tests assert the wiring is present and that the precedence actually behaves.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"

# Resolve bash explicitly rather than letting PATH decide: on Windows, bare
# "bash" can resolve to the WSL launcher, which does not inherit the environment
# a subprocess passes it (WSLENV gates that) and would make this test measure
# WSL interop rather than the fragment. The Makefile runs under this bash.
BASH = shutil.which("bash")

# Targets that reach AWS must carry both guards; `synth` must carry neither,
# because it is part of the hermetic gate and needs no region and no credentials.
_AWS_INVOCATION = re.compile(r"\$\(CDK\)\s+(deploy|destroy|bootstrap)\b")


def _recipes() -> dict[str, str]:
    """Map target name -> its recipe body (tab-indented lines, joined)."""
    recipes: dict[str, list[str]] = {}
    current: str | None = None
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t"):
            if current is not None:
                recipes[current].append(line)
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", line)
        current = match.group(1) if match else None
        if current is not None:
            recipes.setdefault(current, [])
    return {name: "\n".join(body) for name, body in recipes.items()}


def _load_env_fragment() -> str:
    """The LOAD_ENV definition, un-escaped from make's `$$` into shell's `$`."""
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("LOAD_ENV"):
            return line.split("=", 1)[1].strip().replace("$$", "$")
    raise AssertionError("LOAD_ENV is not defined in the Makefile")


def test_every_aws_target_loads_env_and_requires_a_region() -> None:
    aws_targets = {n: b for n, b in _recipes().items() if _AWS_INVOCATION.search(b)}
    assert aws_targets, "no target invokes the CDK CLI — the parser is broken, not the Makefile"
    assert {"bootstrap", "deploy-dev", "deploy", "destroy-dev"} <= set(aws_targets)

    for name, body in aws_targets.items():
        assert "$(LOAD_ENV)" in body, f"{name} reaches AWS without loading .env"
        assert "$(REQUIRE_REGION)" in body, f"{name} reaches AWS without demanding a region"


def test_synth_stays_hermetic() -> None:
    synth = _recipes()["synth"]
    assert "$(LOAD_ENV)" not in synth, "synth must not need .env — it is part of make check"
    assert "$(REQUIRE_REGION)" not in synth, "synth must not need a region or credentials"


@pytest.mark.skipif(BASH is None, reason="the Makefile requires bash anyway")
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "us-west-2|dev"),  # .env fills both gaps
        ({"AWS_REGION": "eu-west-1"}, "eu-west-1|dev"),  # exported region wins
        ({"PII_ERASURE_STAGE": "pr-123"}, "us-west-2|pr-123"),  # CI's stage wins (V3-1)
    ],
)
def test_load_env_precedence_behaves(
    tmp_path: Path, overrides: dict[str, str], expected: str
) -> None:
    # Written with CRLF on purpose: this is what a .env edited on Windows looks
    # like, and "us-west-2\r" is a region that does not exist.
    (tmp_path / ".env").write_bytes(b"AWS_REGION=us-west-2\r\nPII_ERASURE_STAGE=dev\r\n")
    probe = 'echo "$AWS_REGION|$PII_ERASURE_STAGE"'
    script = f"{_load_env_fragment()}\n: ${{AWS_REGION:?unset}}\n{probe}\n"

    env = {k: v for k, v in os.environ.items() if k not in ("AWS_REGION", "PII_ERASURE_STAGE")}
    env.update(overrides)
    result = subprocess.run(
        [BASH, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.skipif(BASH is None, reason="the Makefile requires bash anyway")
def test_missing_region_fails_loudly(tmp_path: Path) -> None:
    """No .env and no exported region must stop, not guess. This is the guard's whole point."""
    script = f"{_load_env_fragment()}\n: ${{AWS_REGION:?is unset}}\necho reached\n"
    env = {k: v for k, v in os.environ.items() if k != "AWS_REGION"}

    result = subprocess.run(
        [BASH, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "reached" not in result.stdout
