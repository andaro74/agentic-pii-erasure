"""Agentic PII erasure: the agent proposes, the saga disposes.

See docs/ARCHITECTURE.md for the design and docs/ROADMAP.md for the build order.

`__version__` is resolved lazily, on attribute access, and never while this module body
runs. The distinction is not cosmetic: `importlib.metadata` answers questions about an
*installed distribution*, and the artifact this package ships to Lambda is a copied source
tree with no `.dist-info` in it. Reading metadata at import time therefore made "was this
pip-installed?" a precondition of importing the library at all — true in every venv, false
in every Lambda, which is why it survived the hermetic gate and killed the deployed one
(V7-1). Deferring the lookup means the CLI, which is always installed, still gets a real
version, and a handler that never asks never pays.

If a runtime component later needs a version string in a receipt or ledger entry, stamp it
into the asset at package time — do not restore the import-time lookup.
"""

from typing import Any

__all__ = ["__version__"]


def __getattr__(name: str) -> Any:
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("agentic-pii-erasure")
        except PackageNotFoundError:
            # Running from a staged Lambda asset rather than an install. Reported as an
            # honest unknown rather than a guess: a fabricated version number in an audit
            # trail is worse than an absent one.
            return "0+unknown"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
