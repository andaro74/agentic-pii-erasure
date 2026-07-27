"""Schema Prospector — probe every candidate for subject-shaped keys.

The sweep is **exhaustive and unconditional**. Every candidate gets a `discover` call on
every run, whatever the priors said, whatever the model thinks, whatever a previous run
found. That is not defensive coding; it is where recall 1.0 comes from, and it is the
reason the recall gate can be a hard gate rather than an aspiration.

The model's contribution here is `scopeHints` — alternate keys worth probing under. A
hint can only *widen* what a participant looks at, so a bad hint costs precision and
never recall. That asymmetry is deliberate and matches ADR-008's: false positives are
caught by the approver in thirty seconds; false negatives are caught by nobody.

**A probe that fails is not a probe that found nothing.** The distinction is the whole
finding of this module. `found: false` is evidence of absence; a Gateway error, a
throttle, or a Cedar denial is *absence of evidence*, and silently folding one into the
other converts an outage into a certificate of erasure. Failures are recorded as
`errored` and the graph refuses to produce a manifest while any remain — fail closed,
because discovery mutates nothing and therefore has nothing to lose by stopping.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pii_erasure.discovery.tools import GatewayError, GatewayToolset


@dataclass(frozen=True)
class ProbeResult:
    """What one participant said about one subject."""

    system_id: str
    found: bool
    artifacts: tuple[dict[str, Any], ...] = ()
    holds: tuple[dict[str, Any], ...] = ()
    #: Set when the probe could not be completed. `found` is then meaningless and
    #: must not be read as "nothing here" — see the module docstring.
    error: str | None = None
    denied: bool = False

    @property
    def errored(self) -> bool:
        return self.error is not None


@dataclass
class SweepReport:
    """The full sweep. Carries failures as loudly as findings."""

    results: tuple[ProbeResult, ...] = ()
    errors: tuple[ProbeResult, ...] = field(default=())

    @property
    def complete(self) -> bool:
        """True only when every probe returned an answer. The manifest gate."""
        return not self.errors

    @property
    def systems_with_data(self) -> tuple[str, ...]:
        return tuple(r.system_id for r in self.results if r.found and not r.errored)


def sweep(
    toolset: GatewayToolset,
    *,
    subject_ref: str,
    saga_id: str,
    candidates: Sequence[str],
    scope_hints: Sequence[str] = (),
) -> SweepReport:
    """Call `discover` on every candidate. No early exit, no skipping, no caching.

    Sequential rather than concurrent on purpose at this milestone: eight calls
    against a Gateway that evaluates Cedar per call is not the bottleneck, and a
    thread pool here would need its own reasoning about partial failure that the
    `errors` tuple gives for free.
    """
    results: list[ProbeResult] = []
    errors: list[ProbeResult] = []
    arguments: dict[str, Any] = {"subjectRef": subject_ref, "sagaId": saga_id}
    if scope_hints:
        arguments["scopeHints"] = list(scope_hints)

    for system_id in candidates:
        try:
            payload = toolset.call(system_id, "discover", dict(arguments))
        except GatewayError as error:
            message = str(error)
            probe = ProbeResult(
                system_id=system_id,
                found=False,
                error=message[:400],
                denied="denied" in message.lower() or "not authorized" in message.lower(),
            )
            errors.append(probe)
            results.append(probe)
            continue
        artifacts = tuple(payload.get("artifacts") or ())
        results.append(
            ProbeResult(
                system_id=system_id,
                # Trust the participant's own `found` when it says so, but never let
                # `found: false` stand while artifacts are present — the contract's
                # two fields disagreeing is a participant bug, and resolving it toward
                # "there is data here" is the only safe direction.
                found=bool(payload.get("found")) or bool(artifacts),
                artifacts=artifacts,
                holds=tuple(payload.get("holds") or ()),
            )
        )
    return SweepReport(results=tuple(results), errors=tuple(errors))
