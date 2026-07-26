"""Byte-stable canonical JSON. CLAUDE.md invariant 4 — the most fragile file here.

Everything downstream of this module inherits its stability. An approval token binds to
``sha256(canonical(manifest))`` (ADR-006 — approval binds to the digest), so two
semantically identical plans that canonicalise to different bytes do not merely produce a
different hash: they invalidate a human approval that is still correct, and the digest
binding stops meaning anything. **Any change to the rules below is a breaking change:
bump `SCHEMA_VERSION` and add a fixture.**

The rules, and where they diverge from RFC 8785 (JCS)
-----------------------------------------------------

1. **Object keys are sorted** by Unicode code point, and are restricted to ASCII
   identifiers. JCS sorts by UTF-16 code unit, which differs from code point ordering
   only above the BMP; refusing non-ASCII keys removes the question rather than
   answering it, and every key in this contract is a camelCase field name anyway.
2. **Arrays preserve order by default.** Order is meaning: `plannedOps` is a sequence
   and §5.2's phase ordering is a plan, not a set. Arrays that are semantically *sets*
   are named in `SET_LIKE_ARRAYS` and sorted; discovery must not produce a different
   digest because a paginated API returned rows in a different order.
3. **Integers only. Floats raise.** JCS specifies ECMAScript `Number::toString`, which
   is intricate and is the classic source of silent digest drift. Every number in this
   contract is a count, so the divergence costs nothing and removes a whole failure
   mode. `bool` is checked before `int`, since Python makes the former a subclass.
4. **Strings are normalised to NFC.** JCS assumes its input is already normalised. Ours
   is not: locators and classifications arrive from eight different AWS services, and
   "café" composed differs bytewise from "café" decomposed while being the same string
   to every human who reads it. Normalising is a divergence that strictly increases
   stability.
5. **Volatile keys are rejected, not dropped.** A canonicaliser that silently strips
   `traceId` is a control that cannot fail: the caller believes provenance was excluded
   because nothing complained. Raising forces the caller to structure the body correctly
   — provenance lives *outside* the digested subtree (ARCHITECTURE §7.1).
6. **Nulls are preserved.** An explicit `null` is not the same as an absent key, and
   collapsing the two would make the digest depend on which serialiser produced the
   input.

No timestamps, run IDs, trace IDs or AgentCore Runtime session IDs can appear inside a
canonicalised body — see rule 5. That is invariant 4's operative clause.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Final, TypeAlias

JSONValue: TypeAlias = "bool | int | str | list[JSONValue] | dict[str, JSONValue] | None"

#: Bump on any behavioural change to the rules above, and add a fixture for the new
#: version. The manifest carries its own ``schemaVersion``; this one versions the bytes.
SCHEMA_VERSION: Final = "1.0.0"

_MAX_DEPTH: Final = 64

_KEY_PATTERN: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

#: Keys that must never appear inside a digested body. Provenance (§7.1) is the block
#: this exists for; ``digest`` and ``signature`` are here because a manifest cannot
#: contain its own digest, and a body that tried would hash a moving target.
VOLATILE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "provenance",
        "discoveredAt",
        "observedAt",
        "appliedAt",
        "createdAt",
        "updatedAt",
        "generatedAt",
        "timestamp",
        "runId",
        "sessionId",
        "runtimeSessionId",
        "traceId",
        "spanId",
        "correlationId",
        "digest",
        "signature",
    }
)

#: Arrays whose order carries no meaning, and the object keys they sort by. An empty
#: tuple means an array of scalars, sorted by value. Anything not named here keeps the
#: order it arrived in — see rule 2. ``expiresAt`` is deliberately not volatile: a hold's
#: expiry is a property of the hold, not of the run that observed it.
SET_LIKE_ARRAYS: Final[dict[str, tuple[str, ...]]] = {
    "artifacts": ("kind", "locator"),
    "residual": ("kind", "locator"),
    "holds": ("holdId",),
    "classification": (),
    "scopeHints": (),
}


class CanonicalisationError(ValueError):
    """Raised when a value cannot be canonicalised byte-stably.

    Carries the JSON path to the offending node and **never the value itself** — this
    exception is reachable from discovery, which reads subject-controlled content, and
    invariant 5 applies to exception messages exactly as it applies to logs.
    """

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def canonical(value: JSONValue) -> bytes:
    """Serialise ``value`` to canonical UTF-8 JSON bytes.

    Deterministic across processes, platforms and Python versions: no dict insertion
    order, no locale, no float formatting, no hash randomisation.
    """
    return _encode(value, path="$", depth=0)


def _encode(value: object, *, path: str, depth: int) -> bytes:
    """Encode one node.

    Typed `object` rather than `JSONValue` on purpose: the public signature states the
    rule, but the values actually arriving here come from `model_dump()` and from JSON
    parsed out of a participant Lambda, so the type checker's guarantee stops at the
    boundary. The float branch below is exactly the case a caller gets wrong, and it must
    produce this module's error rather than a `TypeError` from three frames down.
    """
    if depth > _MAX_DEPTH:
        # Depth is attacker-influenced: discovery canonicalises artifact sets derived
        # from subject-controlled content (threat T3).
        raise CanonicalisationError(path, f"nesting deeper than {_MAX_DEPTH} levels")

    if value is None:
        return b"null"
    if isinstance(value, bool):  # before int — bool is a subclass of int
        return b"true" if value else b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, list):
        return _encode_array(value, path=path, depth=depth)
    if isinstance(value, dict):
        return _encode_object(value, path=path, depth=depth)
    if isinstance(value, float):
        raise CanonicalisationError(
            path, "floats are not canonicalisable — use an integer (see rule 3)"
        )
    raise CanonicalisationError(path, f"unsupported type {type(value).__name__}")


def _encode_string(value: str) -> bytes:
    normalised = unicodedata.normalize("NFC", value)
    # json.dumps with ensure_ascii=False emits exactly RFC 8259's minimal escape set,
    # which is what JCS specifies. Verified against the installed interpreter rather
    # than assumed; the fixtures pin it.
    return json.dumps(normalised, ensure_ascii=False).encode("utf-8")


def _encode_array(value: Sequence[object], *, path: str, depth: int) -> bytes:
    items = [
        _encode(item, path=f"{path}[{index}]", depth=depth + 1) for index, item in enumerate(value)
    ]
    return b"[" + b",".join(items) + b"]"


def _encode_object(value: Mapping[object, object], *, path: str, depth: int) -> bytes:
    # Validate key *types* before sorting: sorted() on mixed-type keys raises TypeError
    # from inside the stdlib, and a canonicaliser reachable from subject-controlled
    # content should fail with its own error and its own path.
    keys = [key for key in value if isinstance(key, str)]
    if len(keys) != len(value):
        raise CanonicalisationError(path, "object keys must be strings")

    parts: list[bytes] = []
    for key in sorted(keys):
        child_path = f"{path}.{key}"
        if not _KEY_PATTERN.match(key):
            raise CanonicalisationError(child_path, "key is not an ASCII identifier (rule 1)")
        if key in VOLATILE_KEYS:
            raise CanonicalisationError(
                child_path,
                "volatile key must not appear inside a digested body (invariant 4) — "
                "keep provenance outside the digested subtree",
            )

        child = value[key]
        if key in SET_LIKE_ARRAYS and isinstance(child, list):
            encoded = _encode_set_like(child, SET_LIKE_ARRAYS[key], path=child_path, depth=depth)
        else:
            encoded = _encode(child, path=child_path, depth=depth + 1)
        parts.append(_encode_string(key) + b":" + encoded)
    return b"{" + b",".join(parts) + b"}"


def _encode_set_like(
    value: Sequence[object], sort_keys: tuple[str, ...], *, path: str, depth: int
) -> bytes:
    """Encode an array whose order is incidental, sorted deterministically.

    Sorting is by the canonical bytes of the declared keys, with the element's own bytes
    as the final tiebreaker — so the ordering is total, and two elements identical on
    their declared keys can never sort unstably.
    """
    encoded: list[tuple[tuple[bytes, ...], bytes]] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        item_bytes = _encode(item, path=item_path, depth=depth + 1)
        if sort_keys:
            if not isinstance(item, dict):
                raise CanonicalisationError(
                    item_path, f"expected an object to sort by {sort_keys!r}"
                )
            key_bytes = tuple(
                _encode(item.get(name), path=f"{item_path}.{name}", depth=depth + 1)
                for name in sort_keys
            )
        else:
            key_bytes = ()
        encoded.append(((*key_bytes, item_bytes), item_bytes))

    encoded.sort(key=lambda pair: pair[0])
    return b"[" + b",".join(item_bytes for _, item_bytes in encoded) + b"]"
