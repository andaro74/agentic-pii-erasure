"""Commit-zero smoke test: the package installs and imports.

Exists so `make check` and CI are green from the first commit (ROADMAP rule 4).
Real tests replace this file's significance from M1 onward.
"""

import pii_erasure


def test_package_imports() -> None:
    assert pii_erasure.__version__ == "0.1.0"
