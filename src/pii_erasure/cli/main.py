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


#: Unbuilt commands accept whatever the Makefile passes and then report their milestone.
#: Without this, click rejects the flags first and the user gets `No such option: --verify`
#: instead of "lands at M5" — a parser error standing in for a roadmap fact, and the M0
#: design intent (every unbuilt command names its milestone and exits non-zero) silently
#: lost. The Makefile documents the eventual interface; these accept it early.
_PASSTHROUGH = {"ignore_unknown_options": True, "allow_extra_args": True}


def _unbuilt(command: str) -> None:
    milestone = _UNBUILT[command]
    _console.print(f"⏳ `erasure {command}` lands at {milestone} — docs/ROADMAP.md")
    raise typer.Exit(code=1)


@app.command()
def seed(
    tenant_id: Annotated[
        str | None,
        typer.Option("--tenant", help="Assert which tenant is being seeded. Never overrides."),
    ] = None,
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

    `--tenant` is an **assertion, not an override**. The seed file is the single source of
    truth for who is being seeded; a flag that could silently disagree with it would put
    one tenant's name on another tenant's data, and every downstream count would be wrong
    in a way no error announces. So a mismatch stops the run.
    """
    from evals.fixtures.generator import (
        FixtureGenerator,
        expected_systems,
        load_seeds,
        reconcile,
    )

    seeds = load_seeds()
    tenant = seeds["tenant"]["displayName"]
    declared = seeds["tenant"]["tenantId"]

    if tenant_id is not None and tenant_id != declared:
        _console.print(
            f"[red]✗[/red] --tenant={tenant_id!r} but seeds/meridian.json declares "
            f"{declared!r}. The seed file is the source of truth; change it there, or "
            f"drop the flag."
        )
        raise typer.Exit(code=1)

    if dry_run:
        _console.print(f"[bold]{tenant}[/bold] — {len(seeds['subjects'])} fabricated subjects")
        for subject_ref, systems in sorted(expected_systems(seeds).items()):
            _console.print(f"  {subject_ref}: {', '.join(sorted(systems))}")
        _console.print("[dim]--dry-run: nothing was written[/dim]")
        return

    generator = FixtureGenerator(clients=_seed_clients(), config=_stack_config(declared))
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
    participant: Annotated[
        str, typer.Option("--participant", help="systemId, e.g. compliance-archive")
    ],
    subject: Annotated[
        str | None, typer.Option("--subject", help="Narrow to one pseudonymous handle.")
    ] = None,
) -> None:
    """Show what a participant is, and what the ground-truth map places in it.

    **Reads the generated map, never the participant itself.** `inspect` answers "what
    *should* be there"; asking the service is what `discover` does (M7). Conflating them
    would let the answer come from the system under test — the failure ADR-020 exists to
    prevent — and it keeps this command free and offline.
    """
    from pii_erasure.contract.registry import get, system_ids

    try:
        spec = get(participant)
    except KeyError:
        _console.print(
            f"[red]✗[/red] unknown participant {participant!r}. "
            f"Registered: {', '.join(system_ids())}"
        )
        raise typer.Exit(code=1) from None

    _console.print(f"[bold]{spec.system_id}[/bold] — {spec.aws_service}")
    _console.print(f"  archetype: {spec.archetype.value}")
    _console.print(f"  lesson:    {spec.lesson}")
    if spec.expects_residual:
        _console.print(
            "  [yellow]residual by design[/yellow]: a correct hard_delete returns "
            "PARTIAL here, never APPLIED (invariant 7)"
        )

    path = Path("evals/fixtures/ground-truth.json")
    if not path.is_file():
        _console.print(f"\n[dim]{path} not found — run `erasure seed` for placement[/dim]")
        return

    subjects = json.loads(path.read_text(encoding="utf-8")).get("subjects", {})
    placed = {
        ref: systems[spec.system_id]
        for ref, systems in sorted(subjects.items())
        if spec.system_id in systems and (subject is None or ref == subject)
    }
    if not placed:
        scope = f" for {subject}" if subject else ""
        _console.print(f"\n[dim]no seeded data in {spec.system_id}{scope}[/dim]")
        return

    _console.print(f"\n  seeded subjects ({len(placed)}):")
    for ref, artifacts in placed.items():
        detail = ", ".join(f"{k}={v}" for k, v in sorted(artifacts.items()))
        _console.print(f"    {ref}: {detail}")


def _stack_config(tenant_id: str) -> dict[str, str]:
    """Resolve deployed resource names from CloudFormation outputs.

    Read from the stack rather than passed in, so a seeder cannot be pointed at the wrong
    environment by a stale environment variable.

    `tenant_id` is threaded in from the seed file rather than read from the environment
    here. It used to default to `PII_ERASURE_TENANT`, which made the environment a second
    source of truth for a value the seed file already declares — the two could diverge and
    stamp the wrong tenant onto every profile item written.
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
        "tenantId": tenant_id,
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


@app.command(context_settings=_PASSTHROUGH)
def ledger(ctx: typer.Context) -> None:
    """Print the hash-chained audit ledger and verify the chain. (M5)"""
    _unbuilt("ledger")


@app.command(context_settings=_PASSTHROUGH)
def discover(ctx: typer.Context) -> None:
    """Run discovery for one subject against the deployed stack. (M7)"""
    _unbuilt("discover")


@app.command(context_settings=_PASSTHROUGH)
def walkthrough(ctx: typer.Context) -> None:
    """The full arc: discover → soft delete → pause → hard delete → certificate. (M8)"""
    _unbuilt("walkthrough")


@app.command(context_settings=_PASSTHROUGH)
def threads(ctx: typer.Context) -> None:
    """List checkpoint threads and their paused state. (M8)"""
    _unbuilt("threads")


@app.command(context_settings=_PASSTHROUGH)
def resume(ctx: typer.Context) -> None:
    """Manually resume a paused saga. (M8)"""
    _unbuilt("resume")


@app.command(context_settings=_PASSTHROUGH)
def approve(ctx: typer.Context) -> None:
    """Approve or deny a paused saga. (M8)"""
    _unbuilt("approve")


if __name__ == "__main__":
    app()
