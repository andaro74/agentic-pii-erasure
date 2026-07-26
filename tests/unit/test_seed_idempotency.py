"""Running `make seed` twice must leave what running it once leaves.

This exists because of V8-7. The seeder is re-run constantly — after a partial failure,
before an eval, when a subject is added — and it was not re-runnable. Two of the failures
were loud (`AdminCreateUser`, `CreateContact` raise the second time) and two were silent
and much worse: `PutObject` on a **versioned** bucket adds a version rather than replacing
one, and an Iceberg `INSERT` appends. After two runs the ground-truth map would claim
`objects=3` while the bucket held six, and the recall gate's denominator would be wrong
with nothing raising.

The fakes below model the one property that matters per service — versioning accumulates,
inserts append, creates collide — and ignore everything else. A fake that reproduced the
whole service would be a worse test, because it would be wrong in ways nobody could see.
`make seed` against the real stack is what proves the writers themselves (ADR-017).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from botocore.exceptions import ClientError

from evals.fixtures.generator import FixtureGenerator, reconcile

SEEDS: dict[str, Any] = {
    "tenant": {"tenantId": "meridian", "displayName": "Meridian Health Collective"},
    "subjects": [
        {
            "subjectRef": "sub_mar_7f3a91c4",
            "displayName": "Marisol Okonkwo",
            "email": "marisol.okonkwo@meridian.invalid",
            "placement": {
                "cognito-identity": {"users": 1},
                "upload-bucket": {"objects": 3, "deleteMarkers": 1},
                "analytics-lake": {"rows": 5},
                "notify-suppression": {"contacts": 1, "suppressionEntries": 1},
                "compliance-archive": {"lockedObjects": 2, "wrappedDeks": 1},
            },
        }
    ],
}


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _FakeCognito:
    def __init__(self) -> None:
        self.users: set[str] = set()

    def admin_create_user(self, **kw: Any) -> dict[str, Any]:
        username = kw["Username"]
        if username in self.users:
            raise _client_error("UsernameExistsException", "AdminCreateUser")
        self.users.add(username)
        return {}

    def admin_get_user(self, **kw: Any) -> dict[str, Any]:
        if kw["Username"] not in self.users:
            raise _client_error("UserNotFoundException", "AdminGetUser")
        return {"Username": kw["Username"]}


class _FakeS3:
    """Versioned, because that is the property that made the seeder wrong.

    One client serves both buckets, as boto3 does, and Object Lock is modelled where it
    actually lives — **on the bucket**. Modelling it per-client (the first version of this
    fake) made the legitimate `upload-bucket` purge look like a violation, which is a fake
    disagreeing with the service about where a control lives.
    """

    def __init__(self, locked: frozenset[str] = frozenset({"archive"})) -> None:
        self.locked = locked
        # Keyed by bucket. The first version of this fake kept one shared list and ignored
        # the Bucket parameter, so the archive's existence probe saw the upload bucket's
        # objects (both use the subjectRef as a prefix) and concluded it had nothing to do.
        self.versions: dict[str, list[dict[str, str]]] = {}
        self.markers: dict[str, list[dict[str, str]]] = {}
        self._n = 0

    def put_object(self, **kw: Any) -> dict[str, Any]:
        self._n += 1
        self.versions.setdefault(kw["Bucket"], []).append(
            {"Key": kw["Key"], "VersionId": f"v{self._n}"}
        )
        return {}

    def delete_object(self, **kw: Any) -> dict[str, Any]:
        self._n += 1
        self.markers.setdefault(kw["Bucket"], []).append(
            {"Key": kw["Key"], "VersionId": f"v{self._n}"}
        )
        return {}

    def delete_objects(self, **kw: Any) -> dict[str, Any]:
        bucket = kw["Bucket"]
        if bucket in self.locked:
            raise AssertionError(
                f"the seeder tried to delete from {bucket}, which is COMPLIANCE-mode "
                "Object Lock — that call cannot succeed against the real service, from "
                "anyone, including root. It must not be made."
            )
        doomed = {(o["Key"], o["VersionId"]) for o in kw["Delete"]["Objects"]}
        self.versions[bucket] = [
            v for v in self.versions.get(bucket, []) if (v["Key"], v["VersionId"]) not in doomed
        ]
        self.markers[bucket] = [
            m for m in self.markers.get(bucket, []) if (m["Key"], m["VersionId"]) not in doomed
        ]
        return {}

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        prefix = kw.get("Prefix", "")
        keys = {
            v["Key"] for v in self.versions.get(kw["Bucket"], []) if v["Key"].startswith(prefix)
        }
        return {"KeyCount": len(keys)}

    def get_paginator(self, name: str) -> Any:
        assert name == "list_object_versions"
        return self

    def paginate(self, **kw: Any) -> list[dict[str, Any]]:
        prefix, bucket = kw.get("Prefix", ""), kw["Bucket"]
        return [
            {
                "Versions": [
                    v for v in self.versions.get(bucket, []) if v["Key"].startswith(prefix)
                ],
                "DeleteMarkers": [
                    m for m in self.markers.get(bucket, []) if m["Key"].startswith(prefix)
                ],
            }
        ]


class _FakeAthena:
    def __init__(self) -> None:
        self.rows: list[str] = []
        self.statements: list[str] = []

    def start_query_execution(self, **kw: Any) -> dict[str, Any]:
        sql = kw["QueryString"]
        self.statements.append(sql)
        if sql.startswith("DELETE"):
            handle = re.search(r"subject_ref = '([^']+)'", sql)
            if handle:
                self.rows = [r for r in self.rows if r != handle.group(1)]
        elif sql.startswith("INSERT"):
            self.rows.extend(re.findall(r"\('([^']+)', 'event-\d+'\)", sql))
        return {"QueryExecutionId": f"q{len(self.statements)}"}

    def get_query_execution(self, **_: Any) -> dict[str, Any]:
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}


class _FakeSes:
    def __init__(self) -> None:
        self.contacts: set[str] = set()
        self.suppressed: set[str] = set()

    def create_contact(self, **kw: Any) -> dict[str, Any]:
        if kw["EmailAddress"] in self.contacts:
            raise _client_error("AlreadyExistsException", "CreateContact")
        self.contacts.add(kw["EmailAddress"])
        return {}

    def put_suppressed_destination(self, **kw: Any) -> dict[str, Any]:
        self.suppressed.add(kw["EmailAddress"])
        return {}


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, **kw: Any) -> dict[str, Any]:
        item = kw["Item"]
        self.items[(item["subject_ref"], item.get("item_id", "-"))] = item
        return {}


class _FakeDynamo:
    def __init__(self) -> None:
        self.tables: dict[str, _FakeTable] = {}

    def Table(self, name: str) -> _FakeTable:  # noqa: N802 - boto3's resource API
        return self.tables.setdefault(name, _FakeTable())


_CONFIG: dict[str, str] = {
    "tenantId": "meridian",
    "userPoolId": "pool",
    "profileTable": "profiles",
    "uploadBucket": "uploads",
    "archiveBucket": "archive",
    "dekRegistryTable": "deks",
    "analyticsTable": "events",
    "analyticsBucket": "lakebucket",
    "analyticsDatabase": "lake",
    "athenaWorkgroup": "wg",
    "contactList": "meridian",
}


@pytest.fixture
def rig() -> tuple[FixtureGenerator, dict[str, Any]]:
    clients: dict[str, Any] = {
        "cognito-idp": _FakeCognito(),
        "dynamodb": _FakeDynamo(),
        "s3": _FakeS3(),
        "athena": _FakeAthena(),
        "sesv2": _FakeSes(),
    }
    config = _CONFIG
    return FixtureGenerator(clients=clients, config=config), clients


def test_a_second_run_produces_an_identical_ground_truth_map(
    rig: tuple[FixtureGenerator, dict[str, Any]],
) -> None:
    generator, _ = rig

    first = generator.run(SEEDS).to_json()
    second = generator.run(SEEDS).to_json()

    assert first == second
    assert reconcile(generator.run(SEEDS), SEEDS) == []


def test_a_second_run_does_not_inflate_the_versioned_bucket(
    rig: tuple[FixtureGenerator, dict[str, Any]],
) -> None:
    """The silent failure. Without the purge, three objects become six and the map lies."""
    generator, clients = rig
    s3 = clients["s3"]

    generator.run(SEEDS)
    after_one = (len(s3.versions["uploads"]), len(s3.markers["uploads"]))
    generator.run(SEEDS)

    assert (len(s3.versions["uploads"]), len(s3.markers["uploads"])) == after_one
    assert after_one == (4, 1), "3 uploads + 1 tombstoned object, and its delete marker"


def test_a_second_run_does_not_duplicate_analytics_rows(
    rig: tuple[FixtureGenerator, dict[str, Any]],
) -> None:
    """Iceberg INSERT appends. The map would understate what discovery finds."""
    generator, clients = rig

    generator.run(SEEDS)
    generator.run(SEEDS)

    assert len(clients["athena"].rows) == 5


def test_the_object_lock_bucket_is_never_asked_to_delete(
    rig: tuple[FixtureGenerator, dict[str, Any]],
) -> None:
    """COMPLIANCE-mode Object Lock refuses deletion from everyone including root, so the
    purge that `upload-bucket` uses is not available here — the seeder must write only
    what is missing. The fake raises if that rule is broken."""
    generator, clients = rig
    s3 = clients["s3"]

    generator.run(SEEDS)
    generator.run(SEEDS)  # a purge attempt against "archive" raises inside the fake

    assert len(s3.versions["archive"]) == 2, "lockedObjects=2, written once, not rewritten"


def test_existing_identities_do_not_stop_a_re_run(
    rig: tuple[FixtureGenerator, dict[str, Any]],
) -> None:
    """The reported error: UsernameExistsException made a partial run unrecoverable."""
    generator, clients = rig
    clients["cognito-idp"].users.add("sub_mar_7f3a91c4")
    clients["sesv2"].contacts.add("sub_mar_7f3a91c4@meridian.invalid")

    truth = generator.run(SEEDS)

    assert "cognito-identity" in truth.systems_for("sub_mar_7f3a91c4")
    assert "notify-suppression" in truth.systems_for("sub_mar_7f3a91c4")


# ─── SES sandbox (V8-11) ──────────────────────────────────────────────────────────────


_SANDBOX_MESSAGE = "Your account is still in the sandbox."


def _sandbox(clients: dict[str, Any]) -> None:
    """Give the rig an account without SES production access.

    Contacts still work — only `PutSuppressedDestination` is refused, and SES reports it
    as a generic `BadRequestException` whose *message* is the only signal.
    """

    def _refuse(**_: Any) -> dict[str, Any]:
        raise ClientError(
            {"Error": {"Code": "BadRequestException", "Message": _SANDBOX_MESSAGE}},
            "PutSuppressedDestination",
        )

    clients["sesv2"].put_suppressed_destination = _refuse


def test_the_sandbox_stops_the_seed_by_default(
    rig: tuple[FixtureGenerator, dict[str, Any]],
) -> None:
    """Failing loudly is right: notify-suppression is invariant 7's worked example, and a
    seed that quietly omitted the suppression entry would look complete while the archetype
    it exists to demonstrate was absent."""
    from evals.fixtures.generator import SesSandboxError

    generator, clients = rig
    _sandbox(clients)

    with pytest.raises(SesSandboxError, match="production access"):
        generator.run(SEEDS)


def test_the_opt_out_records_the_gap_rather_than_hiding_it(
    rig: tuple[FixtureGenerator, dict[str, Any]],
) -> None:
    """Proceeding is allowed; pretending is not. The map must carry the degradation."""
    _, clients = rig
    _sandbox(clients)
    generator = FixtureGenerator(clients=clients, config=_CONFIG, allow_ses_sandbox=True)

    truth = generator.run(SEEDS)
    body = truth.to_json()

    assert truth.degraded, "the gap must be recorded, not merely tolerated"
    assert "degraded" in body
    assert "RESIDUAL_BY_DESIGN" in body["degraded"][0]
    # And the count must tell the truth: no entry was written.
    placement = body["subjects"]["sub_mar_7f3a91c4"]["notify-suppression"]
    assert placement["suppressionEntries"] == 0
    assert placement["contacts"] == 1, "the contact still seeds; only suppression is blocked"


def test_a_non_sandbox_bad_request_still_fails(
    rig: tuple[FixtureGenerator, dict[str, Any]],
) -> None:
    """The match is on the message, so it must stay narrow — any other BadRequest is a
    real defect and must not be absorbed by the sandbox escape hatch."""
    generator, clients = rig

    def _other(**_: Any) -> dict[str, Any]:
        raise _client_error("BadRequestException", "PutSuppressedDestination")

    clients["sesv2"].put_suppressed_destination = _other  # type: ignore[method-assign]

    with pytest.raises(ClientError):
        generator.run(SEEDS)
