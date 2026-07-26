"""Typer entrypoint.

M0 walking skeleton: `--help` and `version` are real; every command whose
milestone has not landed prints "⏳ lands at Mx" and EXITS NON-ZERO. A stub that
pretends success is the defect class docs/VALIDATION.md exists to catch —
baseline finding #2 was a gate that exited 0 while gating nothing.

Real implementations land per docs/ROADMAP.md and replace entries in
_UNBUILT one milestone at a time; the table shrinking to empty is M8.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from pii_erasure import __version__
from pii_erasure.observability.logging import configure_logging

app = typer.Typer(
    name="erasure",
    help="Agentic PII erasure — the agent proposes, the saga disposes. AWS-only (ADR-017).",
    no_args_is_help=True,
)
_console = Console(stderr=True)

# command name -> milestone that makes it real (docs/ROADMAP.md)
_UNBUILT: dict[str, str] = {
    "ledger": "M5",
    "discover": "M7",
    "walkthrough": "M8",
    "threads": "M8",
    "resume": "M8",
    "approve": "M8",
}


@app.callback()
def _init() -> None:
    configure_logging()


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


def _unbuilt(command: str) -> None:
    milestone = _UNBUILT[command]
    _console.print(f"⏳ `erasure {command}` lands at {milestone} — docs/ROADMAP.md")
    raise typer.Exit(code=1)


@app.command()
def seed(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the plan and declared placement; write nothing."),
    ] = False,
    out: Annotated[
        Path, typer.Option("--out", help="Where the generated placement map is written.")
    ] = Path("evals/fixtures/ground-truth.json"),
) -> None:
    """Write fabricated subjects into the deployed participants, emitting ground truth.

    The map is produced by the same pass that performs the writes (invariant 8). It is
    never hand-authored and never derived from what the discovery agent found — either
    would make the recall gate measure itself.
    """
    from evals.fixtures.generator import (
        FixtureGenerator,
        expected_systems,
        load_seeds,
        reconcile,
    )

    seeds = load_seeds()
    tenant = seeds["tenant"]["displayName"]

    if dry_run:
        _console.print(f"[bold]{tenant}[/bold] — {len(seeds['subjects'])} fabricated subjects")
        for subject_ref, systems in sorted(expected_systems(seeds).items()):
            _console.print(f"  {subject_ref}: {', '.join(sorted(systems))}")
        _console.print("[dim]--dry-run: nothing was written[/dim]")
        return

    generator = FixtureGenerator(clients=_seed_clients(), config=_stack_config())
    truth = generator.run(seeds)

    # A declared placement that produced no write would become an invisible recall miss
    # attributed to the agent. Reconcile loudly instead.
    problems = reconcile(truth, seeds)
    if problems:
        for problem in problems:
            _console.print(f"[red]✗[/red] {problem}")
        raise typer.Exit(code=1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(truth.to_json(), indent=2, sort_keys=True), encoding="utf-8")
    _console.print(f"✅ seeded {len(truth.subjects)} subjects · ground truth → {out}")


@app.command()
def inspect(
    subject_ref: Annotated[str, typer.Argument(help="Pseudonymous handle, never raw PII")],
) -> None:
    """Show what the ground-truth map says one subject should have, and where.

    Reads the generated map rather than querying the participants: `inspect` answers "what
    *should* be there", which is the question you need when a recall number looks wrong.
    Asking the participants is what `discover` does, and conflating the two would let the
    answer come from the thing under test.
    """
    path = Path("evals/fixtures/ground-truth.json")
    if not path.is_file():
        _console.print(f"[red]✗[/red] {path} not found — run `erasure seed` first")
        raise typer.Exit(code=1)

    truth = json.loads(path.read_text(encoding="utf-8"))
    placement = truth.get("subjects", {}).get(subject_ref)
    if placement is None:
        _console.print(f"[yellow]no ground truth for {subject_ref}[/yellow]")
        raise typer.Exit(code=1)

    _console.print(f"[bold]{subject_ref}[/bold] — {len(placement)} systems")
    for system_id, artifacts in sorted(placement.items()):
        detail = ", ".join(f"{k}={v}" for k, v in sorted(artifacts.items()))
        _console.print(f"  {system_id}: {detail}")


def _stack_config() -> dict[str, str]:
    """Resolve deployed resource names from CloudFormation outputs.

    Read from the stack rather than passed in, so a seeder cannot be pointed at the wrong
    environment by a stale environment variable.
    """
    import boto3

    stage = os.environ.get("PII_ERASURE_STAGE", "dev")
    cfn = boto3.client("cloudformation")
    outputs: dict[str, str] = {}
    for stack in ("foundation", "participants"):
        described = cfn.describe_stacks(StackName=f"asdp-{stage}-{stack}")["Stacks"][0]
        for output in described.get("Outputs", []):
            outputs[output["OutputKey"]] = output["OutputValue"]

    return {
        "tenantId": os.environ.get("PII_ERASURE_TENANT", "meridian"),
        "userPoolId": outputs["UserPoolId"],
        "profileTable": outputs["ProfileTableName"],
        "billingClusterArn": outputs["BillingClusterArn"],
        "billingSecretArn": outputs["BillingSecretArn"],
        "billingDatabase": "billing",
        "uploadBucket": outputs["UploadBucketName"],
        "archiveBucket": outputs["ComplianceArchiveBucketName"],
        "dekRegistryTable": outputs["DekRegistryTable"],
        "vectorBucket": outputs["VectorBucketName"],
        "vectorIndex": outputs["VectorIndexName"],
        "analyticsDatabase": outputs["AnalyticsDatabaseName"],
        "analyticsTable": outputs["AnalyticsTableName"],
        "athenaWorkgroup": outputs["AthenaWorkgroupName"],
        "contactList": outputs["ContactListName"],
    }


def _seed_clients() -> dict[str, Any]:
    import boto3

    return {
        "cognito-idp": boto3.client("cognito-idp"),
        "dynamodb": boto3.resource("dynamodb"),
        "rds-data": boto3.client("rds-data"),
        "s3": boto3.client("s3"),
        "s3vectors": boto3.client("s3vectors"),
        "athena": boto3.client("athena"),
        "sesv2": boto3.client("sesv2"),
    }


@app.command()
def ledger() -> None:
    """Print the hash-chained audit ledger and verify the chain. (M5)"""
    _unbuilt("ledger")


@app.command()
def discover() -> None:
    """Run discovery for one subject against the deployed stack. (M7)"""
    _unbuilt("discover")


@app.command()
def walkthrough() -> None:
    """The full arc: discover → soft delete → pause → hard delete → certificate. (M8)"""
    _unbuilt("walkthrough")


@app.command()
def threads() -> None:
    """List checkpoint threads and their paused state. (M8)"""
    _unbuilt("threads")


@app.command()
def resume() -> None:
    """Manually resume a paused saga. (M8)"""
    _unbuilt("resume")


@app.command()
def approve() -> None:
    """Approve or deny a paused saga. (M8)"""
    _unbuilt("approve")


if __name__ == "__main__":
    app()
