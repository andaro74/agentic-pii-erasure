"""Commit-zero smoke test: the package installs and imports.

Exists so `make check` and CI are green from the first commit (ROADMAP rule 4).
Real tests replace this file's significance from M1 onward.
"""

import importlib.metadata

import pii_erasure


def test_package_imports() -> None:
    assert pii_erasure.__version__ == importlib.metadata.version("agentic-pii-erasure")
