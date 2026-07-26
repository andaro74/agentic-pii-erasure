"""The Deletion Manifest (ARCHITECTURE §7.1) — the artifact a human approves.

The manifest is the hinge of the whole design: the reasoning plane *produces* one, a
human approves its digest, and the saga *replays* it deterministically (ADR-001). It is
signed with a KMS asymmetric key and immutable once signed — re-planning creates a new
manifest, it never edits one (invariant 3).

Two structural rules matter more than any field:

* **Provenance is quarantined.** Session IDs, trace IDs and timestamps live in one
  `provenance` block, and the digest is computed over everything *except* that block,
  `digest`, and `signature` (see `digest.py`). Re-running discovery tomorrow with a
  different Runtime session must not change the digest of a semantically identical plan,
  or a still-correct approval churns (invariant 4).
* **Boundary objects only.** This module stays framework-free and `boto3`-free —
  `manifest/models.py` is one of the two files a reader should be able to lift wholesale
  (invariant 0). KMS enters in `signing.py`, not here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from pii_erasure.contract import Archetype, ContractModel, Hold, Residual, Verb
from pii_erasure.contract.verbs import Artifact

#: The manifest schema version. Distinct from `canonical.SCHEMA_VERSION`, which versions
#: the *bytes*; this versions the *shape*. Both are digested, so bumping either
#: invalidates outstanding approvals — which is the intended friction (ADR-006).
MANIFEST_SCHEMA_VERSION = "1.0.0"

#: Bare operation names as they appear in `plannedOps` (§7.1) and in the Gateway's
#: published tool names. Derived from the contract's Verb enum rather than retyped, so
#: the two can never drift apart.
_BARE_OPS: frozenset[str] = frozenset(verb.value.split(".", 1)[1] for verb in Verb)


class Provenance(ContractModel):
    """Where this manifest came from. **Never digested** — every field here is either
    volatile or descriptive, and none of it changes what the approver is approving."""

    discovered_at: str = Field(description="ISO-8601; when discovery observed the estate")
    agent_version: str = Field(min_length=1)
    model_id: str | None = None
    runtime_session_id: str | None = Field(
        default=None, description="AgentCore Runtime session — volatile by definition"
    )
    trace_id: str | None = Field(
        default=None, description="Joins to AgentCore Observability; volatile"
    )


class OrderSlot(ContractModel):
    """Where a participant sits in the execution order (§5.2).

    This — not the position in the `participants` array — is the semantic ordering, and
    it IS digested: changing when the shred happens changes what was approved.
    """

    phase: int = Field(ge=1, le=3)
    rank: int = Field(ge=0, description="Within-phase order; lower runs first")


class ManifestParticipant(ContractModel):
    """One system's slice of the plan."""

    system_id: str = Field(min_length=1)
    archetype: Archetype
    artifacts: tuple[Artifact, ...]
    holds: tuple[Hold, ...] = ()
    planned_ops: tuple[str, ...] = Field(min_length=1)
    order: OrderSlot
    delete_method: Literal["PURGE", "CRYPTO_SHRED"] | None = None
    dek_registry_ref: str | None = Field(
        default=None, description="Set when delete_method is CRYPTO_SHRED"
    )
    kms_key_arn: str | None = Field(
        default=None, description="The wrapping CMK — never the shred target (ADR-007)"
    )

    @model_validator(mode="after")
    def _ops_are_real(self) -> ManifestParticipant:
        unknown = [op for op in self.planned_ops if op not in _BARE_OPS]
        if unknown:
            raise ValueError(f"unknown plannedOps {unknown!r} — the contract has five verbs")
        if self.delete_method == "CRYPTO_SHRED" and not self.dek_registry_ref:
            raise ValueError("CRYPTO_SHRED without a dekRegistryRef names no shred target")
        return self


class SignatureBlock(ContractModel):
    """The KMS signature over the digest (§7.1). Algorithm is fixed by the key spec —
    `ECC_NIST_P256` / `ECDSA_SHA_256` — so it is a constant in `signing.py`, not a field
    an attacker could downgrade."""

    kms_key_arn: str = Field(min_length=1)
    value: str = Field(min_length=1, description="Base64 signature bytes")


class Manifest(ContractModel):
    """The central artifact. Frozen; a signed one is edited only by `replan()`."""

    schema_version: str = MANIFEST_SCHEMA_VERSION
    manifest_id: str = Field(min_length=1)
    saga_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1, description="Pseudonymous — never raw PII")
    request_id: str = Field(min_length=1)
    provenance: Provenance
    participants: tuple[ManifestParticipant, ...] = Field(min_length=1)
    legal_holds: tuple[Hold, ...] = ()
    residual_risk: tuple[Residual, ...] = Field(
        default=(),
        description="Known-undeletable, disclosed to the approver BEFORE approval "
        "(invariant 7 starts at plan time)",
    )
    grace_window_days: int = Field(ge=0)
    digest: str | None = Field(default=None, description="sha256:… over the canonical body")
    signature: SignatureBlock | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Manifest:
        system_ids = [participant.system_id for participant in self.participants]
        if len(set(system_ids)) != len(system_ids):
            raise ValueError("duplicate systemId — one participant appears twice in the plan")
        if self.signature is not None and self.digest is None:
            raise ValueError("a signature without a digest signs nothing")
        return self
