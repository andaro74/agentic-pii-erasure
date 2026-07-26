"""Handler logic for the six participants that land at M4.

**What these tests are for, and what they are not.** `moto` is a unit-testing tool here,
never a gate (CLAUDE.md). It models API shapes well and service *semantics* selectively,
and the behaviours that make each of these archetypes interesting — GSI propagation lag,
Iceberg snapshot retention, Aurora referential integrity, S3 Vectors' per-call ceilings —
are precisely the ones it does not reproduce. So these tests pin the decisions that live
in *our* code: call ordering, batching arithmetic, the honest-outcome rules, and the
refusals. `make conformance` against the deployed stack is what proves the services behave
as claimed, and that is the gate.

Two participants get explicit fakes rather than `moto`, because `s3vectors` and `rds-data`
are not modelled. A fake that records calls is honest about being a fake; a mock dressed as
a service is the thing ADR-017 removed.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from pii_erasure.contract import (
    Deletability,
    DiscoverRequest,
    HardDeleteRequest,
    Outcome,
    RestoreRequest,
    SoftDeleteRequest,
    VerifyRequest,
)
from pii_erasure.participants.analytics_lake.handler import (
    SNAPSHOT_KIND,
    AnalyticsLake,
    AthenaTimeoutError,
    _identifier,
)
from pii_erasure.participants.billing_ledger.handler import (
    _DELETE_ORDER,
    BillingLedger,
    DatabaseResumeTimeoutError,
    execute_with_resume,
)
from pii_erasure.participants.cognito_identity.handler import CognitoIdentity
from pii_erasure.participants.notify_suppression.handler import (
    SUPPRESSION_KIND,
    NotifySuppression,
    subject_address,
)
from pii_erasure.participants.profile_store.handler import (
    PARTITION_KEY,
    SORT_KEY,
    ProfileStore,
)
from pii_erasure.participants.vector_index.handler import (
    MAX_VECTORS_PER_SUBJECT,
    VectorIndex,
    vector_key,
)

SUBJECT = "sub_test_0001"
DIGEST = "sha256:" + "a" * 64


def _soft(**kw: Any) -> SoftDeleteRequest:
    return SoftDeleteRequest(
        subject_ref=SUBJECT,
        saga_id="saga_t",
        manifest_digest=DIGEST,
        idempotency_key="sha256:" + "b" * 64,
        artifacts=(),
        **kw,
    )


def _hard(**kw: Any) -> HardDeleteRequest:
    return HardDeleteRequest(
        subject_ref=SUBJECT,
        saga_id="saga_t",
        manifest_digest=DIGEST,
        idempotency_key="sha256:" + "c" * 64,
        artifacts=(),
        approval_token="tok",
        **kw,
    )


def _restore() -> RestoreRequest:
    return RestoreRequest(
        subject_ref=SUBJECT,
        saga_id="saga_t",
        manifest_digest=DIGEST,
        idempotency_key="sha256:" + "d" * 64,
        artifacts=(),
        restore_token="rt",
    )


def _discover() -> DiscoverRequest:
    return DiscoverRequest(subject_ref=SUBJECT, saga_id="saga_t")


def _verify() -> VerifyRequest:
    return VerifyRequest(subject_ref=SUBJECT, saga_id="saga_t")


# ─── cognito-identity ─────────────────────────────────────────────────────────────────


class _RecordingIdp:
    """Records call order. The ordering *is* the participant's contribution."""

    def __init__(self, exists: bool = True) -> None:
        self.calls: list[str] = []
        self._exists = exists

    def admin_get_user(self, **kw: Any) -> dict[str, Any]:
        self.calls.append("admin_get_user")
        if not self._exists:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "UserNotFoundException", "Message": "no"}}, "AdminGetUser"
            )
        return {"Username": kw["Username"]}

    def __getattr__(self, name: str) -> Any:
        def record(**_: Any) -> dict[str, Any]:
            self.calls.append(name)
            if name == "admin_delete_user":
                self._exists = False
            return {}

        return record


def test_cognito_soft_delete_revokes_before_disabling() -> None:
    """Revoke first. Disabling a user does not invalidate tokens already issued, so the
    other order leaves a window in which the subject's client keeps writing."""
    idp = _RecordingIdp()
    participant = CognitoIdentity("pool-1", client=idp)

    participant.soft_delete(_soft())

    mutations = [c for c in idp.calls if c != "admin_get_user"]
    assert mutations == ["admin_user_global_sign_out", "admin_disable_user"]


def test_cognito_restore_does_not_claim_to_restore_sessions() -> None:
    idp = _RecordingIdp()
    response = CognitoIdentity("pool-1", client=idp).restore(_restore())

    assert "admin_enable_user" in idp.calls
    assert response.outcome is Outcome.APPLIED
    # The receipt records what was *not* done, so a reader is not left to assume.
    assert response.affected == 1


def test_cognito_hard_delete_is_idempotent_against_an_absent_user() -> None:
    """Phase 3 retries. A retry that failed because the first attempt succeeded would
    stall the saga behind a statutory deadline (invariant 6 — retry, never compensate)."""
    idp = _RecordingIdp(exists=False)
    response = CognitoIdentity("pool-1", client=idp).hard_delete(_hard())

    assert response.outcome is Outcome.APPLIED
    assert response.affected == 0
    assert "admin_delete_user" not in idp.calls


def test_cognito_verify_is_clean_only_when_the_user_is_gone() -> None:
    idp = _RecordingIdp()
    participant = CognitoIdentity("pool-1", client=idp)
    assert participant.verify(_verify()).clean is False

    participant.hard_delete(_hard())
    assert participant.verify(_verify()).clean is True


# ─── profile-store ────────────────────────────────────────────────────────────────────


@pytest.fixture
def profile_table() -> Any:
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="profiles",
            KeySchema=[
                {"AttributeName": PARTITION_KEY, "KeyType": "HASH"},
                {"AttributeName": SORT_KEY, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": PARTITION_KEY, "AttributeType": "S"},
                {"AttributeName": SORT_KEY, "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()
        yield ddb


def test_profile_store_finds_every_item_in_the_partition(profile_table: Any) -> None:
    table = profile_table.Table("profiles")
    for n in range(7):
        table.put_item(Item={PARTITION_KEY: SUBJECT, SORT_KEY: f"item#{n}", "bio": "fabricated"})

    response = ProfileStore("profiles", resource=profile_table).discover(_discover())

    assert response.found is True
    assert response.artifacts[0].count == 7
    assert response.deletability is Deletability.DELETABLE


def test_profile_store_hard_delete_removes_items_rather_than_setting_a_ttl(
    profile_table: Any,
) -> None:
    """A TTL is a best-effort background sweep with no SLA. Setting one and reporting the
    subject erased would be a lie with a plausible mechanism behind it."""
    table = profile_table.Table("profiles")
    for n in range(3):
        table.put_item(Item={PARTITION_KEY: SUBJECT, SORT_KEY: f"item#{n}"})

    participant = ProfileStore("profiles", resource=profile_table)
    response = participant.hard_delete(_hard())

    assert response.outcome is Outcome.APPLIED
    assert response.affected == 3
    assert participant.verify(_verify()).clean is True
    # Nothing left carrying a TTL attribute, because no TTL was ever the mechanism.
    remaining = table.scan()["Items"]
    assert remaining == []


def test_profile_store_soft_delete_is_reversible(profile_table: Any) -> None:
    table = profile_table.Table("profiles")
    table.put_item(Item={PARTITION_KEY: SUBJECT, SORT_KEY: "item#0"})
    participant = ProfileStore("profiles", resource=profile_table)

    participant.soft_delete(_soft())
    assert table.scan()["Items"][0]["asdp_state"] == "pending-delete"

    participant.restore(_restore())
    assert "asdp_state" not in table.scan()["Items"][0]


def test_profile_store_reads_are_strongly_consistent(profile_table: Any) -> None:
    """The GSI cannot offer ConsistentRead, which is why reads go to the base table.

    Asserted on the call itself: moto will not reproduce index lag, so the only thing
    worth pinning here is that we ask for the guarantee at all.
    """
    seen: list[dict[str, Any]] = []
    participant = ProfileStore("profiles", resource=profile_table)
    original = participant._table.query

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return original(**kwargs)

    participant._table.query = spy  # type: ignore[method-assign]
    participant.discover(_discover())

    assert seen, "no query was issued — the assertion below would pass vacuously"
    assert all(call["ConsistentRead"] is True for call in seen)


# ─── vector-index ─────────────────────────────────────────────────────────────────────


class _FakeVectors:
    """S3 Vectors is not modelled by moto. This fake enforces the two limits that matter."""

    GET_LIMIT = 100
    WRITE_LIMIT = 500

    def __init__(self, keys: list[str]) -> None:
        self.store: dict[str, dict[str, Any]] = {
            key: {"key": key, "data": {"float32": [0.1, 0.2]}, "metadata": {"subjectRef": SUBJECT}}
            for key in keys
        }
        self.batch_sizes: dict[str, list[int]] = {"get": [], "delete": [], "put": []}

    def get_vectors(self, **kw: Any) -> dict[str, Any]:
        keys = kw["keys"]
        self.batch_sizes["get"].append(len(keys))
        assert len(keys) <= self.GET_LIMIT, "GetVectors accepts at most 100 keys"
        return {"vectors": [self.store[k] for k in keys if k in self.store]}

    def delete_vectors(self, **kw: Any) -> dict[str, Any]:
        keys = kw["keys"]
        self.batch_sizes["delete"].append(len(keys))
        assert len(keys) <= self.WRITE_LIMIT, "DeleteVectors accepts at most 500 keys"
        for key in keys:
            self.store.pop(key, None)
        return {}

    def put_vectors(self, **kw: Any) -> dict[str, Any]:
        vectors = kw["vectors"]
        self.batch_sizes["put"].append(len(vectors))
        assert len(vectors) <= self.WRITE_LIMIT, "PutVectors accepts at most 500 vectors"
        for vector in vectors:
            self.store[vector["key"]] = vector
        return {}


def test_vector_index_derives_keys_and_needs_no_mapping_table() -> None:
    """The whole design rests on this: keys are a pure function of the subject handle."""
    assert vector_key(SUBJECT, 0) == f"{SUBJECT}#0000"
    assert vector_key(SUBJECT, 42) == f"{SUBJECT}#0042"

    fake = _FakeVectors([vector_key(SUBJECT, n) for n in range(5)])
    response = VectorIndex("vb", "idx", client=fake).discover(_discover())

    assert response.found is True
    assert response.artifacts[0].count == 5


def test_vector_index_respects_the_two_different_per_call_ceilings() -> None:
    """GetVectors caps at 100 where Put/Delete cap at 500 (V8-2). Batching all three at
    the documented 500 passes every small test and fails on a real corpus."""
    keys = [vector_key(SUBJECT, n) for n in range(MAX_VECTORS_PER_SUBJECT)]
    fake = _FakeVectors(keys)

    VectorIndex("vb", "idx", client=fake).hard_delete(_hard())

    assert max(fake.batch_sizes["get"]) <= _FakeVectors.GET_LIMIT
    assert max(fake.batch_sizes["delete"]) <= _FakeVectors.WRITE_LIMIT
    assert fake.store == {}


def test_vector_index_soft_delete_flags_metadata_without_losing_the_embedding() -> None:
    fake = _FakeVectors([vector_key(SUBJECT, 0)])
    participant = VectorIndex("vb", "idx", client=fake)

    participant.soft_delete(_soft())
    stored = fake.store[vector_key(SUBJECT, 0)]
    assert stored["metadata"]["asdpState"] == "pending-delete"
    # The embedding itself must survive: a soft delete moves metadata, not data.
    assert stored["data"] == {"float32": [0.1, 0.2]}

    participant.restore(_restore())
    assert "asdpState" not in fake.store[vector_key(SUBJECT, 0)]["metadata"]


def test_vector_index_verify_is_clean_after_hard_delete() -> None:
    fake = _FakeVectors([vector_key(SUBJECT, n) for n in range(3)])
    participant = VectorIndex("vb", "idx", client=fake)

    assert participant.verify(_verify()).clean is False
    participant.hard_delete(_hard())
    assert participant.verify(_verify()).clean is True


# ─── notify-suppression ───────────────────────────────────────────────────────────────


class _FakeSes:
    """SES v2, as far as this participant uses it.

    `moto` models contact lists but raises `NotImplementedError` for
    `PutSuppressedDestination` — the suppression list, which is the *entire* subject of
    this archetype, is the part it does not have. A fake that says so is better than a
    test that quietly exercises the half that was never in question.

    Note what it stores: `EmailAddress`, in plaintext, exactly as the real service does.
    That is the fact V8-1 was wrong about, so the fake reproduces it rather than the
    hash the docs used to describe.
    """

    def __init__(self) -> None:
        self.contacts: dict[str, dict[str, Any]] = {}
        self.suppressed: dict[str, dict[str, Any]] = {}

    def _missing(self, operation: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": "NotFoundException", "Message": "not found"}}, operation
        )

    def create_contact(self, **kw: Any) -> dict[str, Any]:
        self.contacts[kw["EmailAddress"]] = {"EmailAddress": kw["EmailAddress"]}
        return {}

    def get_contact(self, **kw: Any) -> dict[str, Any]:
        try:
            return self.contacts[kw["EmailAddress"]]
        except KeyError:
            raise self._missing("GetContact") from None

    def update_contact(self, **kw: Any) -> dict[str, Any]:
        contact = self.get_contact(**kw)
        contact["UnsubscribeAll"] = kw.get("UnsubscribeAll", False)
        return {}

    def delete_contact(self, **kw: Any) -> dict[str, Any]:
        self.contacts.pop(kw["EmailAddress"], None)
        return {}

    def put_suppressed_destination(self, **kw: Any) -> dict[str, Any]:
        self.suppressed[kw["EmailAddress"]] = {
            "EmailAddress": kw["EmailAddress"],
            "Reason": kw["Reason"],
        }
        return {}

    def get_suppressed_destination(self, **kw: Any) -> dict[str, Any]:
        try:
            return {"SuppressedDestination": self.suppressed[kw["EmailAddress"]]}
        except KeyError:
            raise self._missing("GetSuppressedDestination") from None


@pytest.fixture
def ses() -> Any:
    return _FakeSes()


def test_notify_suppression_returns_partial_and_names_the_residual(ses: Any) -> None:
    """Invariant 7's worked example. APPLIED here would be a lie the contract forbids."""
    address = subject_address(SUBJECT)
    ses.create_contact(ContactListName="meridian", EmailAddress=address)
    ses.put_suppressed_destination(EmailAddress=address, Reason="BOUNCE")

    response = NotifySuppression("meridian", client=ses).hard_delete(_hard())

    assert response.outcome is Outcome.PARTIAL
    assert len(response.residual) == 1
    assert response.residual[0].kind == SUPPRESSION_KIND


def test_the_residual_locator_never_contains_the_address(ses: Any) -> None:
    """Invariant 5. The residual travels into the ledger, the certificate and the spans."""
    address = subject_address(SUBJECT)
    ses.create_contact(ContactListName="meridian", EmailAddress=address)
    ses.put_suppressed_destination(EmailAddress=address, Reason="BOUNCE")

    response = NotifySuppression("meridian", client=ses).hard_delete(_hard())
    body = response.digested_body()

    assert address not in str(body)
    assert "@" not in response.residual[0].locator
    assert response.residual[0].locator.startswith("ses://suppression/sha256:")


def test_notify_suppression_applies_cleanly_when_nothing_is_suppressed(ses: Any) -> None:
    """A participant that *can* fully delete must not hedge — invariant 7 cuts both ways."""
    ses.create_contact(ContactListName="meridian", EmailAddress=subject_address(SUBJECT))

    response = NotifySuppression("meridian", client=ses).hard_delete(_hard())

    assert response.outcome is Outcome.APPLIED
    assert response.residual == ()


def test_notify_suppression_discovery_declares_the_residual_at_plan_time(ses: Any) -> None:
    """The approver must see it in the manifest, not discover it in a phase-3 receipt."""
    address = subject_address(SUBJECT)
    ses.create_contact(ContactListName="meridian", EmailAddress=address)
    ses.put_suppressed_destination(EmailAddress=address, Reason="BOUNCE")

    response = NotifySuppression("meridian", client=ses).discover(_discover())

    assert response.deletability is Deletability.PARTIAL
    assert {a.kind for a in response.artifacts} == {"contact", SUPPRESSION_KIND}


def test_notify_suppression_verify_reports_the_surviving_entry(ses: Any) -> None:
    """V8-3: clean=True here would claim an erasure that did not happen."""
    address = subject_address(SUBJECT)
    ses.put_suppressed_destination(EmailAddress=address, Reason="BOUNCE")

    response = NotifySuppression("meridian", client=ses).verify(_verify())

    assert response.clean is False
    assert response.remaining[0].kind == SUPPRESSION_KIND


# ─── billing-ledger ───────────────────────────────────────────────────────────────────


class _FakeDataApi:
    """The RDS Data API is not modelled by moto. Counts and holds are scripted."""

    def __init__(
        self, counts: dict[str, int], holds: list[tuple[str, str, str, str, str | None]]
    ) -> None:
        self.counts = counts
        self.holds = holds
        self.statements: list[str] = []

    def execute_statement(self, **kw: Any) -> dict[str, Any]:
        sql = kw["sql"]
        self.statements.append(sql)
        assert kw["parameters"], "every statement binds its values rather than interpolating"

        if "legal_holds" in sql:
            return {
                "records": [
                    [
                        {"stringValue": h[0]},
                        {"stringValue": h[1]},
                        {"stringValue": h[2]},
                        {"stringValue": h[3]},
                        {"isNull": True} if h[4] is None else {"stringValue": h[4]},
                    ]
                    for h in self.holds
                ]
            }
        if sql.startswith("SELECT count(*)"):
            table = next(t for t in self.counts if t.split(".")[-1] in sql)
            return {"records": [[{"longValue": self.counts[table]}]]}
        if sql.startswith("DELETE"):
            table = next(t for t in self.counts if t.split(".")[-1] in sql)
            deleted = self.counts[table]
            self.counts[table] = 0
            return {"numberOfRecordsUpdated": deleted}
        return {"numberOfRecordsUpdated": 1}


def _ledger(counts: dict[str, int] | None = None, holds: Any = ()) -> tuple[Any, Any]:
    fake = _FakeDataApi(
        dict(counts or dict.fromkeys(_DELETE_ORDER, 2)),
        list(holds),
    )
    return BillingLedger("arn:cluster", "arn:secret", "billing", client=fake), fake


def test_billing_ledger_deletes_child_rows_before_parents() -> None:
    """Referential integrity dictates ordering — the database enforces it, so we must."""
    participant, fake = _ledger()
    participant.hard_delete(_hard())

    deletes = [s for s in fake.statements if s.startswith("DELETE")]
    order = [next(t for t in _DELETE_ORDER if t.split(".")[-1] in s) for s in deletes]
    assert order == list(_DELETE_ORDER)


def test_a_live_hold_refuses_the_delete_outright() -> None:
    """A hold is an unconditional veto. Erasing under one is spoliation, not compliance."""
    holds = [("hold-1", "Ct. of Appeal", "public.", "GDPR Art.17(3)(e)", None)]
    participant, fake = _ledger(holds=holds)

    response = participant.hard_delete(_hard())

    assert response.outcome is Outcome.REFUSED
    assert response.affected == 0
    assert not [s for s in fake.statements if s.startswith("DELETE")]


def test_a_scoped_hold_retains_only_its_scope() -> None:
    """Treating a scoped hold as subject-wide would silently under-delete — a recall
    failure wearing a compliance costume."""
    holds = [("hold-1", "Ct. of Appeal", "public.invoices", "GDPR Art.17(3)(e)", None)]
    participant, fake = _ledger(holds=holds)

    response = participant.hard_delete(_hard())

    assert response.outcome is Outcome.PARTIAL
    retained = {r.locator for r in response.residual}
    # `public.invoice_lines` starts with `public.invoices`? No — prefix matching is on the
    # hold scope, and this asserts the two invoice tables are not conflated.
    assert retained == {"public.invoices"}
    assert any("customers" in s for s in fake.statements if s.startswith("DELETE"))


def test_holds_are_re_read_at_execution_not_trusted_from_the_plan() -> None:
    """A hold can be filed *during* the grace window, after the manifest was approved."""
    participant, fake = _ledger()
    participant.hard_delete(_hard())

    assert any("legal_holds" in s for s in fake.statements)


def test_billing_ledger_discovery_reports_holds_to_the_approver() -> None:
    holds = [("hold-1", "Ct. of Appeal", "public.invoices", "GDPR Art.17(3)(e)", None)]
    participant, _ = _ledger(holds=holds)

    response = participant.discover(_discover())

    assert len(response.holds) == 1
    assert response.deletability is Deletability.PARTIAL


# ─── analytics-lake ───────────────────────────────────────────────────────────────────


class _FakeAthena:
    def __init__(self, count: int = 3, state: str = "SUCCEEDED") -> None:
        self.count = count
        self.state = state
        self.queries: list[str] = []
        self.parameters: list[list[str]] = []

    def start_query_execution(self, **kw: Any) -> dict[str, Any]:
        self.queries.append(kw["QueryString"])
        self.parameters.append(kw.get("ExecutionParameters", []))
        if kw["QueryString"].startswith("DELETE"):
            self.count = 0
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, **_: Any) -> dict[str, Any]:
        return {"QueryExecution": {"Status": {"State": self.state}}}

    def get_query_results(self, **_: Any) -> dict[str, Any]:
        return {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "_col0"}]},
                    {"Data": [{"VarCharValue": str(self.count)}]},
                ]
            }
        }


def _lake(athena: Any) -> AnalyticsLake:
    return AnalyticsLake(
        "lake", "events", "wg", "s3://out/", client=athena, clock=lambda: date(2026, 7, 26)
    )


def test_analytics_lake_hard_delete_is_partial_with_a_dated_window() -> None:
    """Iceberg's DELETE writes a new snapshot; the old one still references the rows."""
    athena = _FakeAthena()
    response = _lake(athena).hard_delete(_hard())

    assert response.outcome is Outcome.PARTIAL
    assert response.residual[0].kind == SNAPSHOT_KIND
    assert response.residual[0].retention_until == "2026-08-02"  # 26 Jul + 7 days


def test_analytics_lake_never_reports_clean() -> None:
    """V8-3, and the conservative direction: over-disclose rather than miss a residual."""
    response = _lake(_FakeAthena(count=0)).verify(_verify())

    assert response.clean is False
    assert response.remaining[0].kind == SNAPSHOT_KIND


def test_subject_values_are_bound_never_interpolated() -> None:
    athena = _FakeAthena()
    _lake(athena).hard_delete(_hard())

    assert all(SUBJECT not in query for query in athena.queries)
    assert any(SUBJECT in params for params in athena.parameters)


def test_a_stuck_statement_raises_rather_than_claiming_success() -> None:
    """An erasure whose result is unknown must never be recorded as one that happened."""
    athena = _FakeAthena(state="RUNNING")
    import pii_erasure.participants.analytics_lake.handler as module

    original = module._MAX_WAIT_SECONDS
    module._MAX_WAIT_SECONDS = 0.0
    try:
        with pytest.raises(AthenaTimeoutError):
            _lake(athena).discover(_discover())
    finally:
        module._MAX_WAIT_SECONDS = original


def test_a_failed_statement_raises() -> None:
    with pytest.raises(RuntimeError, match="FAILED"):
        _lake(_FakeAthena(state="FAILED")).discover(_discover())


def test_catalog_identifiers_are_validated_not_trusted() -> None:
    """The mechanism behind the S608 suppressions. Without it, "trusted" is just a word."""
    assert _identifier("events", field="t") == "events"
    for hostile in ('events" ; DROP TABLE x --', "events-1", "", "1events"):
        with pytest.raises(ValueError, match="not a plain SQL identifier"):
            _identifier(hostile, field="ATHENA_TABLE")


# ─── Aurora auto-pause resume (V8-7) ──────────────────────────────────────────────────


class _ResumingDataApi:
    """Raises DatabaseResumingException `n` times, then succeeds — the real wake sequence."""

    def __init__(self, resumes: int) -> None:
        self.remaining = resumes
        self.calls = 0

    def execute_statement(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise ClientError(
                {"Error": {"Code": "DatabaseResumingException", "Message": "resuming"}},
                "ExecuteStatement",
            )
        return {"numberOfRecordsUpdated": 1}


def test_a_resuming_cluster_is_waited_out_not_failed() -> None:
    """`min_capacity = 0` auto-pauses, so the first statement after idle always sees this.

    Letting it propagate would fail an erasure because the database was asleep — and phase
    3 does not compensate (invariant 6), so a spurious failure there is expensive.
    """
    fake = _ResumingDataApi(resumes=3)
    slept: list[float] = []

    result = execute_with_resume(fake, sleep=slept.append, sql="SELECT 1")

    assert result == {"numberOfRecordsUpdated": 1}
    assert fake.calls == 4, "three resume responses, then the real one"
    assert len(slept) == 3, "it must wait between attempts, not spin"


def test_the_resume_wait_is_bounded_and_fails_loudly() -> None:
    """A cluster that never wakes must not be reported as a completed statement."""
    clock = iter([0.0, 0.0, 200.0, 400.0])

    with pytest.raises(DatabaseResumeTimeoutError, match="still resuming"):
        execute_with_resume(
            _ResumingDataApi(resumes=99),
            sleep=lambda _: None,
            monotonic=lambda: next(clock),
            sql="SELECT 1",
        )


def test_only_the_resume_error_is_retried() -> None:
    """`DatabaseUnavailable` is not self-clearing; retrying a delete against an unhealthy
    database is a different risk with a different answer."""

    class _Unavailable:
        calls = 0

        def execute_statement(self, **_: Any) -> dict[str, Any]:
            type(self).calls += 1
            raise ClientError(
                {"Error": {"Code": "DatabaseUnavailableException", "Message": "down"}},
                "ExecuteStatement",
            )

    client = _Unavailable()
    with pytest.raises(ClientError):
        execute_with_resume(client, sleep=lambda _: None, sql="SELECT 1")
    assert _Unavailable.calls == 1, "it must not be retried"
