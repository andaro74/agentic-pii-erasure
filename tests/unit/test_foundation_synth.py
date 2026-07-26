"""Synth-time assertions on the foundation stack (hermetic — no AWS account).

These live in `make check` because a runtime test would find them too late:

- invariant 14 / threat T9: the DEK registry has PITR explicitly DISABLED and
  joins no backup plan — a restore un-shreds every subject deleted since the
  restore point;
- ADR-010: the ledger archive is Object Lock COMPLIANCE;
- no Lambda in the template has a VPC config (the no-VPC rule, ADR-016);
- prod keeps what dev may destroy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))

from stacks.foundation import FoundationStack


def _synth(stage: str, object_lock_days: int) -> Template:
    app = App()
    stack = FoundationStack(
        app, f"asdp-{stage}-foundation", stage=stage, object_lock_days=object_lock_days
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def dev() -> Template:
    return _synth("dev", object_lock_days=1)


@pytest.fixture(scope="module")
def prod() -> Template:
    return _synth("prod", object_lock_days=2557)


# ── Invariant 14: the DEK registry is never recoverable ──────────────────────


def test_dek_registry_pitr_is_explicitly_disabled(dev: Template) -> None:
    dev.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "asdp-dev-dek-registry",
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": False},
        },
    )


def test_dek_registry_has_no_stream_no_replicas(dev: Template) -> None:
    tables = dev.find_resources(
        "AWS::DynamoDB::Table", {"Properties": {"TableName": "asdp-dev-dek-registry"}}
    )
    assert len(tables) == 1
    props: dict[str, Any] = next(iter(tables.values()))["Properties"]
    assert "StreamSpecification" not in props
    assert "Replicas" not in props
    assert "GlobalSecondaryIndexes" not in props


def test_no_backup_resources_anywhere(dev: Template, prod: Template) -> None:
    for template in (dev, prod):
        template.resource_count_is("AWS::Backup::BackupPlan", 0)
        template.resource_count_is("AWS::Backup::BackupSelection", 0)
        template.resource_count_is("AWS::Backup::BackupVault", 0)


# ── ADR-010: the archive is genuinely immutable ──────────────────────────────


def test_ledger_archive_is_object_lock_compliance(dev: Template) -> None:
    dev.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "ObjectLockEnabled": True,
            "ObjectLockConfiguration": {
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 1}},
            },
        },
    )


def test_prod_archive_retention_is_seven_years(prod: Template) -> None:
    prod.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like(
            {
                "ObjectLockConfiguration": {
                    "ObjectLockEnabled": "Enabled",
                    "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 2557}},
                }
            }
        ),
    )


def test_ledger_table_streams_for_the_archive_export(dev: Template) -> None:
    dev.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "asdp-dev-ledger",
            "StreamSpecification": {"StreamViewType": "NEW_AND_OLD_IMAGES"},
        },
    )


# ── The no-VPC rule and the serverless billing rule ──────────────────────────


def test_no_lambda_has_a_vpc_config(dev: Template) -> None:
    # The only Lambdas at M0 are CDK's auto-delete custom-resource handlers;
    # the assertion still runs over every function so it bites when real ones
    # land at M2+.
    for resource in dev.find_resources("AWS::Lambda::Function").values():
        assert "VpcConfig" not in resource["Properties"]


def test_every_table_is_on_demand(dev: Template) -> None:
    tables = dev.find_resources("AWS::DynamoDB::Table")
    assert len(tables) == 5  # checkpoints, ledger, tombstones, dek-registry, idempotency
    for resource in tables.values():
        assert resource["Properties"]["BillingMode"] == "PAY_PER_REQUEST"


# ── Signing key + checkpoint schema (matches the installed DynamoDBSaver) ────


def test_signing_key_is_asymmetric_sign_verify(dev: Template) -> None:
    dev.has_resource_properties(
        "AWS::KMS::Key", {"KeySpec": "ECC_NIST_P256", "KeyUsage": "SIGN_VERIFY"}
    )


def test_checkpoint_table_matches_the_installed_saver_schema(dev: Template) -> None:
    """PK/SK string keys + `ttl` attribute — read from the installed
    langgraph-checkpoint-aws source, not remembered (ROADMAP rule 3)."""
    dev.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "asdp-dev-checkpoints",
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": Match.array_with(
                [
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ]
            ),
            "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
        },
    )


# ── Prod keeps what dev may destroy ──────────────────────────────────────────


def test_prod_stateful_resources_are_retained(prod: Template) -> None:
    for name in ("asdp-prod-tombstones", "asdp-prod-dek-registry", "asdp-prod-checkpoints"):
        tables = prod.find_resources("AWS::DynamoDB::Table", {"Properties": {"TableName": name}})
        assert len(tables) == 1, name
        assert next(iter(tables.values()))["DeletionPolicy"] == "Retain", name
