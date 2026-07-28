# ADR-025: The discovery Runtime ships as an S3 code zip, not a container image

- **Status:** Accepted — supersedes the container decision implied by ARCHITECTURE §4, `runtime/Dockerfile` in PROJECT-STRUCTURE.md, and [ADR-015](ADR-015-serverless-compute-split.md)'s "Cost 2 — two deployment artifacts", which named an ECR image. ADR-015's *decision* — Runtime for reasoning, Lambda for the saga — is untouched; only the artifact it ships is.
- **Anchors invariants:** CLAUDE.md #0 (framework boundary), and the standing cost constraint ("nothing bills continuously for existing")
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

Every draft of this architecture described AgentCore Runtime as hosting the discovery
subgraph **as a container**: `runtime/Dockerfile`, an ECR repository, an image referenced
by `infra/stacks/runtime.py`. That was correct when it was written — a container was the
only artifact `CreateAgentRuntime` accepted.

It no longer is. `AgentRuntimeArtifact` now takes **either** `containerConfiguration`
(an ECR image URI) **or** `codeConfiguration` — a zip in S3, a Python runtime version,
and an entrypoint:

```jsonc
"codeConfiguration": {
  "code": { "s3": { "bucket": "…", "prefix": "…/runtime.zip" } },
  "runtime": "PYTHON_3_13",
  "entryPoint": ["entrypoint.py"]
}
```

Read from the installed `bedrock-agentcore-control` service model (`2023-06-05`) and the
direct-code-deployment guide, not recalled. `aws-cdk-lib`'s `CfnRuntime` exposes both.

The choice looked like packaging taste. It is not, because of one property this repo has
defended since M0: **`cdk synth` runs inside `make check` and must work with no AWS
account and no Docker daemon.** `Makefile` says it outright — *"No Docker: `cdk synth`
stays hermetic"* — and both participant stacks repeat it.

A container Runtime breaks that. CDK's `DockerImageAsset` builds the image *at synth
time*, so `make check` would acquire a Docker daemon dependency — on every contributor's
machine and in CI — to run a gate whose entire point is that it needs nothing. And
because AgentCore Runtime is **arm64 only**, on the x86 Windows machine this repo is
developed on that means emulated cross-builds in the hermetic gate.

## Decision

**The discovery Runtime deploys as a zip of arm64 wheels uploaded by `aws_s3_assets.Asset`,
via `codeConfiguration`. There is no Dockerfile and no ECR repository.**

Three things follow, and each was verified rather than assumed:

1. **The dependency build is the machinery that already exists.** `make package` already
   cross-installs Lambda assets with `pip install --target --platform … --only-binary=:all:`.
   The Runtime asset is the same recipe with an arm64 tag, and it inherits V9-1's
   determinism work (strip `bin/`, filter `RECORD`) for free.

2. **The platform tag is `manylinux_2_28_aarch64`, not `manylinux2014_aarch64`.** Proven
   the cheap way: `numpy==2.5.1` — pulled in by `langchain-aws` — publishes **no**
   `manylinux2014_aarch64` wheel at all (that tag tops out at 2.2.6), because numpy 2.3+
   requires glibc 2.28. AgentCore Runtime is Amazon Linux 2023 (glibc 2.34), so the newer
   tag is correct and both are accepted. Had this been left to the deploy, it would have
   been V10-5.

3. **The entrypoint implements the HTTP contract directly** — `POST /invocations`,
   `GET /ping`, host `0.0.0.0`, port 8080 — on `http.server.ThreadingHTTPServer`. The
   documented alternative is `@app.entrypoint` from the `bedrock-agentcore` SDK; the
   stdlib is chosen because it adds **zero** dependencies to a package with a 250 MB
   zipped ceiling, and because a server we own can be started in-process by a unit test.
   `/ping` and `/invocations` are therefore assertable in `make check`, which is not true
   of a contract delegated to a vendored framework.

Measured, not estimated: **58 MB zipped, 166 MB unzipped** against limits of 250 MB and
750 MB.

## Consequences

- **Positive — the hermetic gate stays hermetic.** No Docker, no emulation, no daemon.
  The property is preserved by construction rather than by remembering not to break it.
- **Positive — one packaging story.** Contributors learn `pip install --target` once and
  it covers participants, saga, and Runtime. A second, container-shaped path would have
  been the only place in the repo where "how do I ship code" had a different answer.
- **Positive — no ECR.** Image storage bills per GB-month for *existing*, which the
  standing cost constraint forbids without an ADR arguing the floor is worth it. This
  ADR would have had to be that argument; instead the floor is removed. The S3 asset
  bills too, but it is ~58 MB in a bucket the CDK toolkit already provisions.
- **Cost 1 — no control over the base image.** No system packages, no non-Python
  binaries, no custom certificate store. Discovery needs none of these; a future agent
  that does will need a superseding ADR, and should get one rather than a quiet drift
  back to Docker.
- **Cost 2 — the arm64 wheel constraint is now load-bearing and invisible.** A dependency
  that ships no arm64 wheel breaks the deploy, not the build. Mitigated by a unit test
  that asserts the packaging recipe carries both platform tags and `--only-binary=:all:`,
  in the same shape as `test_lambda_asset_determinism.py`.
- **Cost 3 — `entryPoint` is a filename, and filenames are not type-checked.** A rename
  of `entrypoint.py` deploys clean and fails at first invocation. A synth assertion pins
  the name against the file that actually exists.

## Alternatives considered

- **Container image in ECR.** Rejected on the hermetic-synth property above, which is the
  decisive one; the ECR storage floor and arm64 emulation on Windows reinforce it. Worth
  stating plainly: had `codeConfiguration` not existed, the right move would have been to
  accept Docker in `make package` (not in `cdk synth`) and pass a pre-built image URI as a
  stack parameter — a worse design that this ADR is glad to avoid rather than one it
  dismisses.
- **`@app.entrypoint` from the `bedrock-agentcore` SDK.** Rejected for the zip budget and
  for testability, not for quality. It is the right choice for an agent that is not
  fighting a size ceiling and does not want to own an HTTP contract. Revisit if the
  contract grows past what one small module should carry.
- **Keeping the docs' container story and deploying a zip anyway.** Rejected on principle:
  that is precisely the silent divergence the ADR set exists to prevent.

## References

- ARCHITECTURE.md §4 (the Runtime), §14 (no local mode) · PROJECT-STRUCTURE.md `runtime/`
- [ADR-016](ADR-016-serverless-durability.md) (why pins are exact) ·
  [ADR-017](ADR-017-real-aws-participants.md) (deployment is the product) ·
  [ADR-021](ADR-021-s3-vectors-for-cost.md) (the no-idle-cost constraint this follows)
- [VALIDATION.md](../VALIDATION.md) V9-1 (asset determinism), V10-1..V10-4 (service-side
  rules the hermetic gate could not see — the reason the platform tag was proven early)
