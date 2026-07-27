"""AgentCore Memory topology priors — invariant 13 and ADR-019.

Memory is a **cross-subject** surface by design: its whole value is that something
learned deleting subject A improves the deletion of subject B. That is also exactly why
it is dangerous. Anything subject-shaped written here leaks across the one boundary this
architecture exists to protect (threat T7), and it leaks *silently* — nothing fails, a
later run simply retrieves a fact about someone else's data.

So this module is written to make the dangerous thing hard rather than to make the safe
thing convenient:

* **Writes are records, not conversation.** `BatchCreateMemoryRecords` stores exactly the
  text given. The alternative — `CreateEvent` plus an extraction strategy — asks a model
  to decide what is worth remembering from a transcript that contains artifact locators
  and subject handles. That is a PII leak with an LLM in the loop, and no scrubber placed
  after it can be trusted. Verified against the installed `bedrock-agentcore` model.
* **A suspect write is rejected, never sanitised** (`observability.redact.reject_if_pii`).
  Silently storing a near-miss is the failure mode; a loud refusal is the save.
* **Structure is checked, not just content.** The PII patterns catch emails and phone
  numbers. They do not catch `sub_a3f9…`, which is pseudonymous and therefore passes
  every content rule while being precisely the thing that must never be here. The
  subject-handle rule below is the one that does the real work.

**Priors are advisory, never authoritative.** :func:`ordered_candidates` returns a
*permutation* of the registry — never a subset. The type makes the guarantee: it always
returns every system, and there is no parameter that changes that. A prior may say "look
at `vector-index` first"; nothing may say "do not look at `billing-ledger`". If a warm
prior ever lowers recall, that is a P1 and the prior is wrong, not the gate (ADR-008).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pii_erasure.contract.registry import system_ids
from pii_erasure.observability.redact import PIIRejectedError, reject_if_pii

#: Namespace under which a tenant's topology lives. No subject axis exists in this
#: path, and adding one would be the whole leak.
NAMESPACE_TEMPLATE = "/topology/{tenant}"

#: A pseudonymous subject handle: `sub_` followed by hex. Content-scrubbing cannot see
#: this — it is not PII, it is a *pointer* to a subject, which is what invariant 13
#: actually forbids. Matched case-insensitively and anywhere in the text.
_SUBJECT_HANDLE = re.compile(r"\bsub[_-][0-9a-f]{4,}\b", re.IGNORECASE)

#: Other per-subject shapes with no business in a cross-subject store. `saga_…` and
#: `man_…` identify one erasure; a digest identifies one plan; a hold identifies one
#: subject's legal position.
_FORBIDDEN_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("subject_handle", _SUBJECT_HANDLE),
    ("saga_id", re.compile(r"\bsaga[_-][0-9a-z]{4,}\b", re.IGNORECASE)),
    ("manifest_id", re.compile(r"\bman[_-][0-9a-z]{4,}\b", re.IGNORECASE)),
    ("digest", re.compile(r"\bsha256:[0-9a-f]{8,}", re.IGNORECASE)),
    ("hold_id", re.compile(r"\b(?:LIT|HOLD)-[0-9]{2,}\b", re.IGNORECASE)),
    ("arn", re.compile(r"\barn:aws[a-z-]*:[a-z0-9-]+:", re.IGNORECASE)),
)


class MemoryWriteRejectedError(ValueError):
    """A prior was refused before it reached AgentCore Memory (invariant 13).

    Never echoes the offending text: this message goes to logs, which is the other
    place the value must not appear.
    """

    def __init__(self, rule: str) -> None:
        super().__init__(
            f"memory write rejected by rule {rule!r} — Memory holds topology only "
            "(invariant 13, ADR-019). The write is refused, not sanitised."
        )
        self.rule = rule


@dataclass(frozen=True)
class Prior:
    """One topology fact about a tenant. Deliberately not parameterised by subject."""

    tenant: str
    text: str
    kind: str = "topology"

    def namespace(self) -> str:
        return NAMESPACE_TEMPLATE.format(tenant=self.tenant)


def assert_topology_only(text: str) -> str:
    """Return *text* iff it carries nothing subject-shaped. Raise otherwise.

    Two layers, and the second is the one that matters. `reject_if_pii` catches raw
    PII — an email that should never have been near this path. The shape rules below
    catch the *pseudonymous* identifiers that pass every content check: `sub_a3f9…`
    contains no PII and is exactly what must not be stored.
    """
    try:
        reject_if_pii(text)
    except PIIRejectedError as error:
        raise MemoryWriteRejectedError(error.rule) from error
    for rule, pattern in _FORBIDDEN_SHAPES:
        if pattern.search(text):
            raise MemoryWriteRejectedError(rule)
    return text


class TopologyMemory:
    """Read and write tenant topology priors. All writes pass the scrubber first."""

    def __init__(self, *, memory_id: str, client: Any, tenant: str) -> None:
        self._memory_id = memory_id
        self._client = client
        self._tenant = tenant

    @property
    def namespace(self) -> str:
        return NAMESPACE_TEMPLATE.format(tenant=self._tenant)

    def write(self, priors: Sequence[Prior], *, now: datetime | None = None) -> int:
        """Store priors. Rejects the whole batch if any single one is suspect.

        Whole-batch rejection is deliberate: dropping the offending record and storing
        the rest would make a leak attempt indistinguishable from a clean write in the
        return value, and the caller would never learn it had a bug.
        """
        if not priors:
            return 0
        for prior in priors:
            assert_topology_only(prior.text)
        stamp = now or datetime.now(timezone.utc)
        records = [
            {
                # Deterministic per (tenant, text) so a re-run updates rather than
                # duplicates — priors are learned repeatedly by construction.
                "requestIdentifier": f"{self._tenant}:{abs(hash(prior.text)):016x}",
                "namespaces": [prior.namespace()],
                "content": {"text": prior.text},
                "timestamp": stamp,
                "metadata": {"kind": prior.kind, "tenant": self._tenant},
            }
            for prior in priors
        ]
        self._client.batch_create_memory_records(memoryId=self._memory_id, records=records)
        return len(records)

    def read(self, *, query: str = "systems holding subject data", top_k: int = 20) -> list[str]:
        """Retrieve this tenant's priors as plain strings. Never raises on absence:
        a cold tenant has no priors, and that is the normal first run."""
        response = self._client.retrieve_memory_records(
            memoryId=self._memory_id,
            namespace=self.namespace,
            searchCriteria={"searchQuery": query, "topK": top_k},
        )
        out: list[str] = []
        for record in response.get("memoryRecordSummaries", []):
            text = (record.get("content") or {}).get("text")
            if isinstance(text, str) and text:
                out.append(text)
        return out

    def all_records(self) -> list[str]:
        """Every record in the tenant namespace — what `no_pii_in_memory` reads back.

        Distinct from :meth:`read`, which is a semantic query and may legitimately
        return a subset. An evaluator that asserts "nothing subject-shaped is stored"
        has to see everything, or it grades a sample and reports a certainty.
        """
        out: list[str] = []
        token: str | None = None
        for _page in range(50):
            kwargs: dict[str, Any] = {"memoryId": self._memory_id, "namespace": self.namespace}
            if token:
                kwargs["nextToken"] = token
            response = self._client.list_memory_records(**kwargs)
            for record in response.get("memoryRecordSummaries", []):
                text = (record.get("content") or {}).get("text")
                if isinstance(text, str):
                    out.append(text)
            token = response.get("nextToken")
            if not token:
                break
        return out


def ordered_candidates(priors: Iterable[str]) -> tuple[str, ...]:
    """Every registered system, reordered by what the tenant's priors mention first.

    The return is always a permutation of `system_ids()` — same length, same members.
    That is the mechanism behind "priors are advisory": there is no argument to this
    function that can shorten the result, so no prior can cause a system to be skipped.
    A prior naming a decommissioned system is harmless here; it sorts a name that is
    not in the registry and drops out.
    """
    registry = tuple(system_ids())
    text = " ".join(priors).lower()
    # Ordered by where the prior first mentions each system, not by registry order:
    # a prior reading "vector-index mirrors profile-store and must be purged first"
    # is *saying* to look at the index first, and ranking by registry position would
    # discard exactly the signal the prior carries.
    mentioned = sorted(
        (s for s in registry if s.lower() in text),
        key=lambda s: text.index(s.lower()),
    )
    rest = [s for s in registry if s not in mentioned]
    ordered = tuple(mentioned + rest)
    assert set(ordered) == set(registry), "priors must reorder the sweep, never shorten it"
    return ordered
