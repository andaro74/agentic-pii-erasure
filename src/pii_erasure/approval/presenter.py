"""The approval view — a control, not a UI nicety (ARCHITECTURE §8.4).

An approval screen that dumps 400 artifacts guarantees rubber-stamping, and a
rubber-stamped gate is worse than no gate: it produces an audit trail asserting that a
human reviewed something nobody read. The T2 tier (§8.1) makes human approval mandatory
for *every* hard delete, so the volume of approvals is exactly the pressure that turns
review into reflex.

This module is the countermeasure, and it works by ordering rather than by exhortation.

**Anomalies and residual risk come first, inventory comes last and truncated.** The
section order is a module constant, the renderer emits sections in that order, and a test
fails on the reverse. It is deliberately not a parameter: "render this however you like"
is how an inventory ends up on top.

**What the approver is protected from, specifically:**

| Failure | What the view does |
|---|---|
| A plan that quietly grew a system | `unseen-system` anomaly, against the tenant's history |
| A plan that quietly *lost* one | `missing-system` anomaly — the recall smell a human can catch |
| Approving an irreversible act unaware | `crypto-shred` anomaly + the irreversibility countdown |
| Approving without knowing what survives | residual risk is section one, always |
| A baseline that could not be computed | `baseline-unavailable` anomaly — never silence |

That last row is the one worth arguing about. The tempting behaviour when there is no
tenant history is to show no anomalies, which reads to a tired approver as *nothing
unusual here*. An absent comparison is not a clean comparison, and the difference matters
most on the first deletion in a tenant — when there is no history and the blast radius is
least understood. So it is surfaced as a finding with its own severity.

**Nothing here is a decision.** The presenter computes severity and the §8.1 tier; it
never approves, never denies, and never filters a participant out of the view. A
`high`-severity anomaly is a reason to look, not a verdict — the human's judgement is the
control, and this module exists to make that judgement possible rather than to replace it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pii_erasure.manifest import Manifest
from pii_erasure.observability.redact import scrub_mapping

#: Fixed, and not a parameter. Inventory last is the whole point (§8.4).
SECTION_ORDER: tuple[str, ...] = (
    "residualRisk",
    "anomalies",
    "irreversibility",
    "blastRadius",
    "inventory",
)

#: A plan is only compared against a tenant that has enough history to compare against.
#: Below this, "you have never deleted from this system before" is true of everything and
#: the signal is noise — so the view says the baseline is too thin instead of crying wolf.
MIN_DELETIONS_FOR_BASELINE = 5

#: A system present in at least this share of past deletions is expected; its absence is
#: worth a human's attention. Set high on purpose — a *missing* system is the anomaly
#: class most likely to be a false alarm, and an approver who learns to dismiss this one
#: learns to dismiss the row above it too.
EXPECTED_SYSTEM_SHARE = 0.9

#: How many inventory rows reach the view. The rest are counted, never silently dropped —
#: "and 380 more" is information; a list that simply stops is a lie by omission.
INVENTORY_LIMIT = 20

_SEVERITIES = ("high", "medium", "low")


@dataclass(frozen=True)
class TenantBaseline:
    """What this tenant's previous erasures touched.

    Injected rather than fetched: the presenter stays pure and hermetically testable, and
    *where* history comes from — the ledger, a projection, an operator's assertion — is
    the caller's problem. `deletions_observed` is the denominator; `systems_seen` maps a
    `system_id` to how many of those deletions touched it.
    """

    deletions_observed: int = 0
    systems_seen: Mapping[str, int] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        return self.deletions_observed >= MIN_DELETIONS_FOR_BASELINE

    def share(self, system_id: str) -> float:
        if not self.deletions_observed:
            return 0.0
        return self.systems_seen.get(system_id, 0) / self.deletions_observed


@dataclass(frozen=True)
class Anomaly:
    kind: str
    severity: str
    detail: str
    system_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "detail": self.detail,
            "systemId": self.system_id,
        }


def anomalies(manifest: Manifest, baseline: TenantBaseline) -> tuple[Anomaly, ...]:
    """Everything about this plan that differs from what this tenant usually does.

    Ordered by severity, then by kind, so the ordering is a property of the findings
    rather than of the iteration order of a dict.
    """
    found: list[Anomaly] = []
    planned = {p.system_id for p in manifest.participants}

    if not baseline.is_usable:
        found.append(
            Anomaly(
                kind="baseline-unavailable",
                severity="medium",
                detail=(
                    f"only {baseline.deletions_observed} prior deletion(s) recorded for this "
                    f"tenant — too few to say what is normal. This plan has not been compared "
                    f"against anything; read the blast radius in full."
                ),
            )
        )
    else:
        for system_id in sorted(planned):
            if system_id not in baseline.systems_seen:
                found.append(
                    Anomaly(
                        kind="unseen-system",
                        severity="high",
                        system_id=system_id,
                        detail=(
                            f"{system_id} was touched by none of the last "
                            f"{baseline.deletions_observed} deletions in this tenant"
                        ),
                    )
                )
        for system_id in sorted(baseline.systems_seen):
            if system_id not in planned and baseline.share(system_id) >= EXPECTED_SYSTEM_SHARE:
                found.append(
                    Anomaly(
                        kind="missing-system",
                        severity="high",
                        system_id=system_id,
                        detail=(
                            f"{system_id} appeared in {baseline.share(system_id):.0%} of prior "
                            f"deletions and is absent from this plan — either the subject has "
                            f"no data there, or discovery missed it"
                        ),
                    )
                )

    for participant in manifest.participants:
        if participant.delete_method == "CRYPTO_SHRED":
            found.append(
                Anomaly(
                    kind="crypto-shred",
                    severity="high",
                    system_id=participant.system_id,
                    detail=(
                        f"{participant.system_id} is erased by destroying its data key. The "
                        f"ciphertext remains and becomes permanently undecryptable — there is "
                        f"no restore path after this runs."
                    ),
                )
            )

    if manifest.residual_risk:
        found.append(
            Anomaly(
                kind="residual-risk",
                severity="medium",
                detail=(
                    f"{len(manifest.residual_risk)} artifact(s) will survive this erasure by "
                    f"design. Section one states which, and why."
                ),
            )
        )

    for hold in manifest.legal_holds:
        found.append(
            Anomaly(
                kind="legal-hold",
                severity="high",
                detail=(
                    f"hold {hold.hold_id} ({hold.basis}, {hold.authority}) scopes "
                    f"{hold.scope}. Holds are re-checked after the grace window and veto "
                    f"execution then, regardless of this approval."
                ),
            )
        )

    return tuple(sorted(found, key=lambda a: (_SEVERITIES.index(a.severity), a.kind)))


def required_tier(manifest: Manifest) -> str:
    """The §8.1 gate tier this plan lands in.

    T3 — two-person, privacy plus legal — is triggered by holds, crypto-shred, or
    disclosed residual risk. Computed here so the API and the CLI cannot disagree about
    what a plan requires; enforcing it is the caller's job, and `api.py` does it.
    """
    shreds = any(p.delete_method == "CRYPTO_SHRED" for p in manifest.participants)
    if manifest.legal_holds or manifest.residual_risk or shreds:
        return "T3"
    return "T2"


def present(
    manifest: Manifest,
    *,
    baseline: TenantBaseline | None = None,
    inventory_limit: int = INVENTORY_LIMIT,
) -> dict[str, Any]:
    """The approval view, sections in `SECTION_ORDER`.

    `baseline=None` is not an error and is not treated as an empty history that happens
    to match: it produces the `baseline-unavailable` anomaly, same as a history too thin
    to use.

    The whole view goes through `scrub_mapping` on the way out (invariant 5). A manifest
    should carry no PII — participants return locators and counts — so a `[REDACTED]`
    appearing in an approval view means a participant is leaking, and the right response
    is to fix the participant. Scrubbing here means the leak does not also reach the
    approver's screen, their browser history, and the ledger entry recording what they saw.
    """
    baseline = baseline or TenantBaseline()
    found = anomalies(manifest, baseline)

    rows: list[dict[str, Any]] = []
    for participant in manifest.participants:
        for artifact in participant.artifacts:
            rows.append(
                {
                    "systemId": participant.system_id,
                    "kind": artifact.kind,
                    "locator": artifact.locator,
                    "count": artifact.count,
                    "classification": list(artifact.classification),
                }
            )

    view = {
        "sagaId": manifest.saga_id,
        "subjectRef": manifest.subject_ref,
        "manifestDigest": manifest.digest,
        "tier": required_tier(manifest),
        "sections": list(SECTION_ORDER),
        # ── 1. what survives ─────────────────────────────────────────────
        "residualRisk": [
            {
                "systemLocator": r.locator,
                "kind": r.kind,
                "count": r.count,
                "reason": r.reason,
            }
            for r in manifest.residual_risk
        ],
        # ── 2. what is unusual ───────────────────────────────────────────
        "anomalies": [a.as_dict() for a in found],
        # ── 3. what cannot be undone ─────────────────────────────────────
        "irreversibility": {
            "graceWindowDays": manifest.grace_window_days,
            "cryptoShredSystems": [
                p.system_id for p in manifest.participants if p.delete_method == "CRYPTO_SHRED"
            ],
            "note": (
                "Phase 3 never compensates. After the grace window elapses there is no "
                "restore path — a failed hard delete is retried or halted, never rolled back."
            ),
        },
        # ── 4. how much ──────────────────────────────────────────────────
        "blastRadius": [
            {
                "systemId": p.system_id,
                "archetype": p.archetype.value,
                "artifactCount": sum(a.count for a in p.artifacts),
                "deleteMethod": p.delete_method,
                "plannedOps": list(p.planned_ops),
            }
            for p in manifest.participants
        ],
        # ── 5. the rows, last and bounded ────────────────────────────────
        "inventory": {
            "shown": rows[:inventory_limit],
            "totalRows": len(rows),
            "omitted": max(0, len(rows) - inventory_limit),
        },
    }
    return scrub_mapping(view)


def render_text(view: Mapping[str, Any]) -> str:
    """The same view as terminal text, for `erasure approve --show`.

    Shares `SECTION_ORDER` with `present()` rather than re-listing the sections, so the
    CLI cannot drift into showing an inventory first while the API shows anomalies first.
    """
    lines: list[str] = [
        f"saga {view['sagaId']}  ·  subject {view['subjectRef']}  ·  tier {view['tier']}",
        f"digest {view['manifestDigest']}",
    ]
    for section in SECTION_ORDER:
        lines.append("")
        lines.append(f"── {section} ──")
        lines.extend(_render_section(section, view.get(section)))
    return "\n".join(lines)


def _render_section(section: str, body: Any) -> list[str]:
    if section == "residualRisk":
        rows = list(body or [])
        if not rows:
            return ["  nothing survives this erasure"]
        return [f"  {r['kind']} {r['systemLocator']} x{r['count']} — {r['reason']}" for r in rows]
    if section == "anomalies":
        rows = list(body or [])
        if not rows:
            return ["  none"]
        return [f"  [{r['severity']}] {r['kind']}: {r['detail']}" for r in rows]
    if section == "irreversibility":
        body = body or {}
        shreds = ", ".join(body.get("cryptoShredSystems") or []) or "none"
        return [
            f"  grace window: {body.get('graceWindowDays')} days",
            f"  crypto-shred: {shreds}",
            f"  {body.get('note', '')}",
        ]
    if section == "blastRadius":
        return [
            f"  {r['systemId']} ({r['archetype']}) — {r['artifactCount']} artifact(s), "
            f"{'/'.join(r['plannedOps'])}"
            for r in (body or [])
        ]
    body = body or {}
    lines = [
        f"  {r['systemId']}: {r['kind']} {r['locator']} x{r['count']}"
        for r in body.get("shown", [])
    ]
    if body.get("omitted"):
        lines.append(f"  … and {body['omitted']} more row(s) of {body['totalRows']}")
    return lines or ["  no artifacts"]


def baseline_from_history(history: Sequence[Mapping[str, Any]]) -> TenantBaseline:
    """Fold prior manifests' system lists into a baseline.

    Takes the minimum it needs — `{"systems": [...]}` per past deletion — so the caller
    can build it from the ledger, a projection, or a test fixture without this module
    knowing which.
    """
    seen: dict[str, int] = {}
    for record in history:
        for system_id in dict.fromkeys(record.get("systems") or []):
            seen[system_id] = seen.get(system_id, 0) + 1
    return TenantBaseline(deletions_observed=len(history), systems_seen=seen)
