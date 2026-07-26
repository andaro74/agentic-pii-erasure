# ADR-022: Canonical JSON is a documented subset of RFC 8785, not RFC 8785

- **Status:** Accepted
- **Anchors invariants:** CLAUDE.md #4 (canonicalisation must be byte-stable)
- **Refines:** [006](ADR-006-approval-binds-to-digest.md) — the digest that approval binds to is the output of this
- **Baseline:** architecture v0.2, milestone M1

## Context

[ADR-006](ADR-006-approval-binds-to-digest.md) makes an approval token bind to
`sha256(canonical(manifest))`. That decision is only as strong as `canonical()`: if two
semantically identical plans can produce different bytes, a still-correct human approval
silently stops matching, and the operator's recourse is to re-approve a plan they already
approved. The failure is worse than a crash, because it appears legitimate.

**RFC 8785 (JSON Canonicalization Scheme)** is the obvious answer and mostly the right
one. Three of its properties are not, for this system:

1. **Number serialisation.** JCS specifies ECMAScript `Number::toString`, an intricate
   shortest-round-trip algorithm. Implementations disagree at the edges, and a
   disagreement surfaces as a digest that differs between the planner and the verifier.
   Every number in this contract is a count.
2. **String normalisation.** JCS assumes its input is already Unicode-normalised. Ours is
   not: locators, classifications and hold scopes arrive from eight different AWS
   services with no shared normalisation policy, and `café` composed differs bytewise
   from `café` decomposed while being the same string to every human who reads it.
3. **Nothing about provenance.** JCS is a serialiser; it has no opinion on *what* belongs
   in the digested body. Invariant 4's operative clause — no timestamps, run IDs, trace
   IDs or Runtime session IDs — has to live somewhere.

## Decision

`contract/canonical.py` implements a **subset of JCS with three deliberate divergences**,
each of which strictly narrows what is representable rather than reinterpreting it:

| Rule | JCS | Here | Why |
|---|---|---|---|
| Numbers | ECMAScript `Number::toString` | **Integers only; floats raise** | Removes the whole class rather than implementing it. Costs nothing: every number in the contract is a count. |
| Strings | Assumed normalised | **Normalised to NFC** | The inputs genuinely are not normalised, and the difference is invisible in every editor. |
| Object keys | Sorted by UTF-16 code unit | Sorted by code point, **keys restricted to ASCII identifiers** | The two orderings differ only above the BMP; restricting keys removes the question instead of answering it. |
| Provenance | Out of scope | **Volatile keys are rejected, not stripped** | See below. |

The fourth row is the one that matters most. A canonicaliser that silently drops
`traceId` is a control that cannot fail: the caller believes provenance was excluded
precisely because nothing complained. Raising forces the body to be *structured*
correctly — provenance lives outside the digested subtree (ARCHITECTURE §7.1) — and makes
the mistake visible at the point it is made.

Arrays keep their order by default, because order is meaning: `plannedOps` is a sequence
and §5.2's phase ordering is a plan. Arrays that are semantically sets are named
explicitly in `SET_LIKE_ARRAYS` and sorted, so a paginated API returning rows in a
different order cannot churn a digest. §8.3's "any change to the plan — even reordering —
invalidates the approval" holds exactly because the two cases are distinguished.

`SCHEMA_VERSION` versions the bytes, separately from the manifest's own `schemaVersion`.
Golden fixtures pin the exact output for known inputs, so a change to any rule turns them
red rather than silently re-digesting every outstanding approval.

## Consequences

**Good.** The digest is stable across processes, platforms and Python versions — asserted
under three `PYTHONHASHSEED` values, not assumed. Four failure modes are unrepresentable
rather than merely discouraged: float drift, normalisation drift, key-ordering drift, and
provenance leaking into a signed body. The rules fit on one screen, which matters for the
most fragile file in the repository.

**Bad — and worth stating plainly.** This is **not** interoperable with a general JCS
implementation. A third party who canonicalises the same manifest with a stock JCS
library gets different bytes whenever a string needed normalising, and an error where we
would have accepted a float. If cross-implementation verification of the manifest digest
ever becomes a requirement — an external auditor recomputing it, say — this ADR is what
must be superseded, and the successor has to specify the number algorithm rather than
avoid it.

**Also bad.** `VOLATILE_KEYS` is a name-based denylist, so a provenance field named
something new goes undetected until someone adds it to the list. The structural defence —
provenance in its own subtree, excluded by construction — is the primary control; the
denylist is the backstop for when a caller flattens it by mistake. Neither is weakened on
the grounds that the other exists.

## Alternatives rejected

**Implement JCS exactly.** Buys interoperability nobody has asked for, at the cost of
implementing and testing ECMAScript number formatting in the file that must not be
subtly wrong. Revisit if an external verifier appears.

**`json.dumps(sort_keys=True, separators=…)`.** The one-liner. Ships with `ensure_ascii`
ambiguity, no normalisation, float acceptance, and no provenance rule — and it *looks*
canonical, which is the dangerous part. It would have passed every test we thought to
write before writing the fixtures.

**Strip volatile keys instead of raising.** Convenient, and it converts invariant 4 from
an enforced property into a hope. Explicitly rejected: see the fourth row above.

**Canonicalise with a schema rather than a key denylist.** Stronger, and it is where this
goes if the denylist proves leaky — but the manifest models land in M3, and building the
schema-driven version first would have meant writing it against a shape that did not
exist yet.
