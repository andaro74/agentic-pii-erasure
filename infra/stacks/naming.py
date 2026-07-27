"""AgentCore resource names — the control plane has **two** conventions, not one.

Most of this stack names things `asdp-{stage}-{thing}`, and AgentCore's Gateway and
Target accept exactly that: their `name` shapes are `([0-9a-zA-Z][-]?){1,48}` and
`([0-9a-zA-Z][-]?){1,100}`. Hyphens are fine, so M4 deployed without anyone noticing
there was a question.

Policy, PolicyEngine and AgentRuntime do **not** take that form. Their `name` shapes are
`[A-Za-z][A-Za-z0-9_]*`, capped at 48 characters — a leading letter, then letters,
digits and underscores. A hyphen is rejected outright, and the rejection arrives from
CloudFormation during change-set validation: after `cdk synth` has passed, after the
assets have uploaded, minutes into a deploy the human paid for.

So the two conventions get translated here rather than assumed, and the translation
fails loudly at synth time. `make check` runs `cdk synth`, which means a name the
control plane would refuse cannot reach a deploy.

Both constants below were read from the installed `bedrock-agentcore-control` service
model (`2023-06-05`) rather than recalled, and `tests/unit/test_naming.py` re-reads that
model and asserts they still match — so if AWS tightens or relaxes the shape, the build
says so instead of the next deploy.
"""

from __future__ import annotations

import re

#: `[A-Za-z][A-Za-z0-9_]*`, 1 to 48 chars. Applies to `PolicyName`, `PolicyEngineName` and
#: `AgentRuntimeName` (the last lands at M7 — the Runtime is not built yet, but it will
#: reach for this helper rather than rediscovering the constraint the expensive way).
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
IDENTIFIER_MAX_LENGTH = 48

#: What a hyphenated name is translated through. `.` appears in nothing today, but a
#: policy filename is a plausible source of one and silently dropping it would produce
#: two resources competing for one name.
_TRANSLATED = str.maketrans({"-": "_", ".": "_", "/": "_"})


class NameConstraintError(ValueError):
    """A generated name the AgentCore control plane would reject.

    Raised at synth, not swallowed and not truncated: a name quietly trimmed to 48
    characters can collide with another trimmed name, and two Cedar policies sharing
    one name means one of them is not deployed.
    """


def agentcore_identifier(*parts: str) -> str:
    """Join `parts` into an underscore-delimited AgentCore identifier.

    ``agentcore_identifier("asdp", "dev", "05-discovery-never-mutates")``
    → ``asdp_dev_05_discovery_never_mutates``

    Raises `NameConstraintError` rather than repairing anything. The failure modes of
    repair are worse than the failure mode of stopping: truncation collides, and
    stripping an illegal character can make two distinct sources converge.
    """
    name = "_".join(part for part in parts if part).translate(_TRANSLATED)
    if not IDENTIFIER_PATTERN.match(name):
        raise NameConstraintError(
            f"{name!r} is not a legal AgentCore identifier: the control plane requires "
            f"{IDENTIFIER_PATTERN.pattern} (a leading letter, then letters, digits and "
            f"underscores) — built from {parts!r}"
        )
    if len(name) > IDENTIFIER_MAX_LENGTH:
        raise NameConstraintError(
            f"{name!r} is {len(name)} characters; AgentCore allows "
            f"{IDENTIFIER_MAX_LENGTH}. Shorten the source rather than truncating here — "
            f"two truncated names can collide, and a collision silently drops a policy"
        )
    return name
