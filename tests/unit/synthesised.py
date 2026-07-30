"""The synthesised CloudFormation templates, loaded once and checked for staleness.

Two tests read `infra/cdk.out` rather than constructing a stack in-process, deliberately:
they assert things about the **whole app** — that no stage stack contains a budget, that
every stack in the tree is covered by the cost-floor rule — and constructing stacks by hand
would bypass `infra/app.py`, which is where the wiring being asserted actually lives.

Reading a build artefact has two failure modes, and both have bitten this repo:

* **It might not exist.** `infra/cdk.out` is gitignored, CI checks out clean, and
  `make check` used to run `test` *before* `synth` — so these checks passed locally on a
  directory left over from a previous run and would have failed in CI on their first
  outing. `make check` now runs `synth` first, and `tests/unit/test_makefile_env.py`
  asserts that ordering so it cannot quietly go back (V13-7).
* **It might be stale.** A direct `pytest tests/unit` after editing a stack would otherwise
  assert against yesterday's template and pass — the silent version, and the one no
  ordering fix removes. So the loader compares mtimes and fails loudly instead.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SYNTH = REPO / "infra" / "cdk.out"
INFRA_SOURCES = (REPO / "infra" / "app.py", REPO / "infra" / "stacks")


def _newest_source_mtime() -> float:
    newest = 0.0
    for source in INFRA_SOURCES:
        paths = [source] if source.is_file() else source.rglob("*.py")
        for path in paths:
            if "__pycache__" not in path.parts:
                newest = max(newest, path.stat().st_mtime)
    return newest


@lru_cache(maxsize=1)
def templates() -> dict[str, dict[str, Any]]:
    """`{template filename: parsed template}` for every stack the default app synthesises.

    Fails rather than returning an empty or stale mapping — an empty one would make every
    assertion downstream vacuously true, which is the precise shape of a gate that cannot
    gate.
    """
    paths = sorted(SYNTH.glob("asdp-*.template.json"))
    if not paths:
        pytest.fail(
            f"no synthesised templates in {SYNTH}. Run `make synth` — these checks read the "
            f"template because that is what CloudFormation acts on. `make check` runs synth "
            f"before the tests for this reason."
        )

    oldest_template = min(path.stat().st_mtime for path in paths)
    if oldest_template < _newest_source_mtime():
        pytest.fail(
            f"{SYNTH} is older than infra/. These checks would pass against a template that "
            f"no longer matches the stacks — run `make synth`."
        )

    return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in paths}
