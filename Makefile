.DEFAULT_GOAL := help
SHELL := /bin/bash

# venv layout differs by platform: Windows puts executables in .venv/Scripts,
# POSIX in .venv/bin. Auto-detect so the same Makefile works on Windows,
# WSL, Linux and CI. Falls back to bin/ before the venv exists.
VENV_BIN := $(if $(wildcard .venv/Scripts),.venv/Scripts,.venv/bin)
PY := $(VENV_BIN)/python
PIP := $(VENV_BIN)/pip
RUFF := $(VENV_BIN)/ruff
MYPY := $(VENV_BIN)/mypy
PYTEST := $(VENV_BIN)/pytest

# `make lock` runs pip-tools in a throwaway venv with BOTH tools pinned. pip-tools
# imports pip's private internals, so an unpinned install against an arbitrary pip is a
# mechanism that breaks on somebody else's machine and nowhere else (V12-7). `pip<26` is
# the constraint that matters: pip 26 removed `stdlib_pkgs`.
LOCK_ENV := .lockenv
LOCK_ENV_BIN := $(if $(filter .venv/Scripts,$(VENV_BIN)),$(LOCK_ENV)/Scripts,$(LOCK_ENV)/bin)
LOCK_TOOLS := "pip<26" "pip-tools==7.6.0"

# src and tests exist from commit zero; infra lands at M0, evals and seeds at
# M4/M7. Lint only the dirs that exist so `make check` is green at commit zero,
# and pick them up automatically the moment they appear — the same "becomes
# mandatory when it lands" rule the milestone gates use (docs/ROADMAP.md, rule 4).
# Not a silencing guard.
LINT_DIRS := src tests $(wildcard infra evals seeds scripts)

# Milestone-gated targets used to print "⏳ lands at Mx" instead of failing, so
# `make check` and CI were green from commit zero. **Every stage has now landed and
# the guards are gone** (M9's "every gate now mandatory"). They were correct while
# the files were unwritten and a hazard the moment they were not: delete a suite and
# its gate printed a milestone note and exited 0. Never re-add one to silence a
# failing gate (docs/ROADMAP.md, rule 4).
#
# TWO KINDS OF GATE (docs/ROADMAP.md). There is no local mode — ADR-017.
#   HERMETIC : lint, test, policy-test, synth. No AWS account. This is `make check`.
#   DEPLOYED : conformance, integration, eval, chaos, walkthrough. Needs a
#              deployed stack, costs money, and is run by a human.

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ─── Setup ────────────────────────────────────────────────────────────────────
.PHONY: install
install: ## Create venv and install (lockfile-constrained) with dev extras
	python3 -m venv .venv
	$(PY) -m pip install -U pip
	@# requirements.lock pins the transitive layer under the invariant-9 pins
	@# (ADR-016). It constrains rather than installs, so extras resolve freely
	@# where the lock is silent. Regenerate ONLY via make lock + upgrade-canary.
	@if [ -f requirements.lock ]; then \
		$(PY) -m pip install -c requirements.lock -e ".[dev,infra,otel,cedar]"; \
	else \
		$(PY) -m pip install -e ".[dev,infra,otel,cedar]"; \
	fi
	@test -f .env || cp .env.example .env
	@echo "✅ next: make check · then open docs/ROADMAP.md"

.PHONY: lock
lock: ## Regenerate requirements.lock from the runtime deps (invariant 9 mechanism)
	@# pip-tools reaches into pip's PRIVATE internals — `pip._internal.utils.compat`
	@# .stdlib_pkgs, which pip 26 removed. `pip install pip-tools` unpinned against
	@# whatever pip the venv happened to have therefore broke the one mechanism
	@# invariant 9 names, and broke it only for whoever had upgraded pip (V12-7).
	@#
	@# So both tools are pinned, and installed into a venv of their own: locking must not
	@# depend on which pip a developer has, and must not change the project venv's pip as
	@# a side effect of regenerating a lockfile. Torn down either way — it is a tool, not
	@# an environment.
	@rm -rf $(LOCK_ENV)
	$(PY) -m venv $(LOCK_ENV)
	@$(LOCK_ENV_BIN)/python -m pip install -q $(LOCK_TOOLS)
	$(LOCK_ENV_BIN)/python -m piptools compile --quiet --strip-extras -o requirements.lock pyproject.toml
	@rm -rf $(LOCK_ENV)
	@echo "requirements.lock regenerated — a langgraph/checkpoint-aws bump requires make upgrade-canary"

# ─── Quality — HERMETIC, no AWS account ───────────────────────────────────────
.PHONY: lint
lint: ## ruff + mypy
	$(RUFF) check $(LINT_DIRS)
	$(RUFF) format --check src tests
	$(MYPY) src

.PHONY: fmt
fmt: ## Autoformat
	$(RUFF) format $(LINT_DIRS)
	$(RUFF) check --fix $(LINT_DIRS)

.PHONY: test
test: ## Unit tests — contract, reducers, handler logic (moto), CDK IAM assertions
	$(PYTEST) tests/unit

.PHONY: policy-test
policy-test: ## Cedar policy tests + engine/Cedar divergence test (M6)
	$(PYTEST) tests/unit/test_policies.py

# cdk.json says "python app.py" for humans with an activated venv; make always
# passes the venv interpreter explicitly so synth works from a bare shell.
CDK_APP := "$(abspath $(VENV_BIN)/python)" app.py

# The CDK CLI is pinned: an older cached CLI cannot read the cloud-assembly
# schema emitted by the pinned aws-cdk-lib, and npx will happily serve one.
CDK := npx -y aws-cdk@2.1133.0

# ─── .env is authoritative for the AWS-touching targets ───────────────────────
# make does not read .env, so every variable in it used to be inert: a user
# could set AWS_REGION=eu-west-1 there and deploy to whatever their profile
# said, because the stacks are environment-agnostic and the CDK CLI resolves
# the region from ambient credentials. A setting that describes an intention
# without a mechanism is the defect class docs/VALIDATION.md exists to catch.
#
# Shell-set variables WIN over .env, deliberately: CI passes AWS_REGION and
# PII_ERASURE_STAGE as job env, and `make install` writes a .env from the
# example — sourcing blindly would resurrect V3-1 (two concurrent PRs
# destroying each other's "ephemeral" stack).
#
# `make synth` does NOT load it. Synth needs no region and no credentials, and
# it must stay that way — it is part of the hermetic gate.
# `tr -d '\r'` because a .env edited on Windows carries CRLF, and a region of
# "us-west-2\r" is a region that does not exist. Precedence is applied to the two
# variables CI actually overrides, by name — snapshotting the whole environment
# with `eval "$$(export -p)"` looks more general but breaks on Windows, where
# names like ProgramFiles(x86) are not valid shell identifiers.
LOAD_ENV = if [ -f .env ]; then _r="$$AWS_REGION"; _s="$$PII_ERASURE_STAGE"; set -a; . <(tr -d '\r' < .env); set +a; export AWS_REGION="$${_r:-$$AWS_REGION}" PII_ERASURE_STAGE="$${_s:-$$PII_ERASURE_STAGE}"; fi
REQUIRE_REGION = : $${AWS_REGION:?is unset — set it in .env (see .env.example) or export it}

.PHONY: synth
synth: ## CDK synth. Free, no credentials. IAM assertions live in tests/unit. (M0)
	cd infra && $(CDK) synth --quiet --app '$(CDK_APP)'

.PHONY: check
check: lint test policy-test synth ## What CI runs on every commit. HERMETIC — no AWS account.

# ─── Deployment — HUMAN-ONLY, costs money ─────────────────────────────────────
# Denied to Claude Code in .claude/settings.json. Read infra/README.md first.
# Nothing in the stack bills a continuous floor (ADR-021), so an idle dev stack
# is cheap — but an Object Lock bucket with a long retention period cannot be
# torn down by anyone, including root, until that retention expires.
#
# STAGE names the stack instance. Resolution order, highest first:
#   1. make deploy-dev STAGE=foo      explicit override
#   2. PII_ERASURE_STAGE in the shell  CI sets STAGE=pr-<run_id> per run
#   3. PII_ERASURE_STAGE in .env       the human's default
#   4. "dev"
# Ephemeral eval stacks must actually be ephemeral — with a shared hardcoded
# stage, two concurrent PRs would deploy into and then destroy each other's.
STAGE ?= $(PII_ERASURE_STAGE)
RESOLVE_STAGE = stage="$(STAGE)"; stage="$${stage:-$${PII_ERASURE_STAGE:-dev}}"

# Participant Lambda asset. Built for Lambda's platform, not the host's — a pydantic
# wheel compiled for win_amd64 imports fine here and fails in the runtime. --only-binary
# is what makes that failure loud at build time instead of at conformance time.
# No Docker: `cdk synth` stays hermetic, and this target only runs before a deploy.
LAMBDA_ASSET := infra/build/participants
SAGA_ASSET := infra/build/saga
LAMBDA_PLATFORM := manylinux2014_x86_64
LAMBDA_PY := 3.12

# The discovery Runtime asset (M7, ADR-025). AgentCore Runtime is arm64-only and
# runs Amazon Linux 2023 (glibc 2.34), so manylinux_2_28 is both correct and
# required: numpy 2.3+ ships no manylinux2014_aarch64 wheel at all.
RUNTIME_ASSET := infra/build/runtime
RUNTIME_PLATFORMS := manylinux_2_28_aarch64 manylinux2014_aarch64
RUNTIME_PY := 3.13
RUNTIME_ENTRYPOINT := entrypoint.py

# Every staged deploy asset, in ONE place. The cleanup steps below all iterate this:
# V10-5 was three hand-maintained lists (bin/, RECORD, __pycache__) and a fourth asset
# that only made it into two of them. A list that must be updated in N places is a
# list that will be updated in N-1.
ASSETS := $(LAMBDA_ASSET) $(SAGA_ASSET) $(RUNTIME_ASSET)
# The saga asset's framework pins MUST match pyproject.toml exactly (invariant 9) —
# a unit test compares these strings against the pyproject pins verbatim, so a bump
# that touches only one of the two fails `make check` instead of deploying a Lambda
# whose checkpoint serialization differs from the one the tests exercised.
SAGA_PINS := "langgraph==1.2.10" "langgraph-checkpoint-aws==1.2.0"

.PHONY: package
package: ## Stage the deploy assets (participants + saga + discovery Runtime).
	@# Clear the staging dirs but KEEP .gitkeep: it is a tracked file, and an
	@# `rm -rf` here shows up as a deletion that a careless `git add -A` commits,
	@# which breaks `cdk synth` for the next person to clone without packaging.
	@mkdir -p $(LAMBDA_ASSET) $(SAGA_ASSET) $(RUNTIME_ASSET)
	@find $(LAMBDA_ASSET) -mindepth 1 -maxdepth 1 ! -name .gitkeep -exec rm -rf {} +
	@find $(SAGA_ASSET) -mindepth 1 -maxdepth 1 ! -name .gitkeep -exec rm -rf {} +
	@find $(RUNTIME_ASSET) -mindepth 1 -maxdepth 1 ! -name .gitkeep -exec rm -rf {} +
	$(PY) -m pip install --quiet --target $(LAMBDA_ASSET) \
		--platform $(LAMBDA_PLATFORM) --python-version $(LAMBDA_PY) \
		--implementation cp --only-binary=:all: \
		"pydantic>=2.9,<3" "structlog>=24.1"
	@cp -r src/pii_erasure $(LAMBDA_ASSET)/
	@# -c requirements.lock: the transitive layer under the invariant-9 pins includes
	@# ormsgpack — the checkpoint serializer's wire format. The asset must ship the
	@# exact versions the test suite ran against, not whatever resolves freshest.
	$(PY) -m pip install --quiet --target $(SAGA_ASSET) \
		--platform $(LAMBDA_PLATFORM) --python-version $(LAMBDA_PY) \
		--implementation cp --only-binary=:all: \
		-c requirements.lock \
		"pydantic>=2.9,<3" "structlog>=24.1" $(SAGA_PINS)
	@# The Lambda runtime ships boto3/botocore; bundling a second copy doubles the
	@# asset for zero benefit. The saga's own AWS calls run fine on the runtime's.
	@rm -rf $(SAGA_ASSET)/boto3* $(SAGA_ASSET)/botocore*
	@cp -r src/pii_erasure $(SAGA_ASSET)/
	@# ── the discovery Runtime (M7, ADR-025) ──────────────────────────────────
	@# arm64, because AgentCore Runtime is arm64-only, and manylinux_2_28 because
	@# numpy 2.3+ (pulled in by langchain-aws) publishes NO manylinux2014_aarch64
	@# wheel — that tag tops out at numpy 2.2.6. Both tags are passed: pip accepts
	@# the first that matches, so older pure-arm64 wheels still resolve. This was
	@# proven with `pip download` before a deploy, not discovered during one.
	$(PY) -m pip install --quiet --target $(RUNTIME_ASSET) \
		$(foreach tag,$(RUNTIME_PLATFORMS),--platform $(tag)) \
		--python-version $(RUNTIME_PY) --implementation cp --only-binary=:all: \
		-c requirements.lock \
		"pydantic>=2.9,<3" "structlog>=24.1" $(SAGA_PINS) "langchain>=1.3,<2" "langchain-aws>=1.6,<2"
	@cp -r src/pii_erasure $(RUNTIME_ASSET)/
	@# entryPoint is a FILENAME, and filenames are not type-checked (ADR-025 cost 3).
	@# A rename deploys clean and fails at first invocation, so the file is copied to
	@# the zip root under the exact name the stack declares, and a synth assertion
	@# pins the two together.
	@cp src/pii_erasure/runtime/entrypoint.py $(RUNTIME_ASSET)/$(RUNTIME_ENTRYPOINT)
	@# AgentCore Runtime REFUSES an artifact carrying these outright ("Python cache
	@# files that are incompatible with the target runtime") — x86 Windows bytecode
	@# does not run on arm64 Linux, and pip byte-compiles on install by default. For
	@# the Lambda assets the same files were merely non-deterministic; here they are
	@# a failed deploy. Same strip, higher stakes (V10-5).
	@find $(ASSETS) -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ASSETS) -name '*.pyc' -o -name '*.pyo' -delete 2>/dev/null || true
	@# ── console scripts must not ship (V9-1) ─────────────────────────────────
	@# `pip install --target` materialises entry-point wrappers into bin/ even under
	@# --platform: Windows .exe launchers (a zip with a stub prepended, carrying an
	@# embedded timestamp) and POSIX scripts whose shebang is the BUILD machine's
	@# interpreter path. Nothing in Lambda ever runs a console script, so they are
	@# dead weight — but the real damage is that the .exe bytes differ on every
	@# build, which makes the CDK asset hash non-deterministic and the deployed-code
	@# staleness preflight report drift that does not exist. Stripping them is what
	@# makes the asset a function of the SOURCE rather than of the build machine and
	@# the minute it ran. The RECORD filter keeps the metadata honest about it.
	@rm -rf $(addsuffix /bin,$(ASSETS))
	@find $(ASSETS) -name RECORD -exec sed -i '\|^\.\..*/bin/|d' {} + 2>/dev/null || true
	@echo "✅ staged $(LAMBDA_ASSET) ($$(du -sh $(LAMBDA_ASSET) | cut -f1)) + $(SAGA_ASSET) ($$(du -sh $(SAGA_ASSET) | cut -f1)) + $(RUNTIME_ASSET) ($$(du -sh $(RUNTIME_ASSET) | cut -f1))"

.PHONY: bootstrap
bootstrap: ## ⚠️ ONE-TIME per account+region: create the CDK toolkit stack. Human-only.
	@$(LOAD_ENV); \
	$(REQUIRE_REGION); \
	acct=$$(aws sts get-caller-identity --query Account --output text) || \
		{ echo "❌ no usable AWS credentials — configure them first"; exit 1; }; \
	echo "bootstrapping aws://$$acct/$$AWS_REGION"; \
	$(CDK) bootstrap "aws://$$acct/$$AWS_REGION"

.PHONY: preflight
preflight: ## Check region-specific facts cdk synth cannot know. Read-only, free.
	@# `cdk synth` validates shape, never availability. The CDK engine-version enum lists
	@# versions that exist *somewhere*, at the time the library was published — not what
	@# this region offers today. VER_16_6 synthesised clean and CloudFormation rejected it
	@# ten minutes into a deploy, because 16.6 ships only as 16.6-limitless (V8-5).
	@# One API call answers that before the rollback rather than after.
	@#
	@# The version is read from the attribute access, not a bare VER_ token: the comment
	@# above the engine declaration names the version that FAILED, and a looser pattern
	@# reads that instead — which had this check reporting the old version as unavailable.
	@# Correct answer, wrong question.
	@$(LOAD_ENV); \
	$(REQUIRE_REGION); \
	want=$$(grep -oE 'AuroraPostgresEngineVersion\.VER_[0-9]+_[0-9]+' infra/stacks/participants.py \
		| head -1 | sed 's/.*VER_//; s/_/./'); \
	if [ -z "$$want" ]; then echo "❌ could not read the engine version from the stack"; exit 1; fi; \
	echo "preflight: aurora-postgresql $$want in $$AWS_REGION"; \
	found=$$(aws rds describe-db-engine-versions --engine aurora-postgresql \
		--engine-version "$$want" --query 'DBEngineVersions[0].EngineVersion' \
		--output text 2>/dev/null); \
	if [ "$$found" != "$$want" ]; then \
		echo "❌ aurora-postgresql $$want is NOT available in $$AWS_REGION."; \
		echo "   available 16.x (standard, non-limitless):"; \
		aws rds describe-db-engine-versions --engine aurora-postgresql \
			--query 'DBEngineVersions[?starts_with(EngineVersion,`16.`)].EngineVersion' \
			--output text 2>/dev/null | tr '\t' '\n' | grep -v limitless | sed 's/^/     /'; \
		echo "   update AuroraPostgresEngineVersion in infra/stacks/participants.py"; \
		exit 1; \
	fi; \
	ses=$$(aws sesv2 get-account --query ProductionAccessEnabled --output text 2>/dev/null); 	if [ "$$ses" != "True" ]; then 		echo "⚠️  SES is in the SANDBOX (ProductionAccessEnabled=$$ses)."; 		echo "   PutSuppressedDestination is refused, so notify-suppression cannot be"; 		echo "   seeded with a suppression entry and the RESIDUAL_BY_DESIGN archetype"; 		echo "   (invariant 7's worked example) will not be demonstrated."; 		echo "   Fix: SES console -> Account dashboard -> Request production access."; 		echo "   Or:  make seed ALLOW_SES_SANDBOX=1  (records the gap in ground truth)"; 	fi; 	echo "✅ preflight passed"

.PHONY: deploy-dev
deploy-dev: package preflight ## ⚠️ Deploy a dev-shaped stack (STAGE=dev by default). Costs money.
	@$(LOAD_ENV); \
	$(REQUIRE_REGION); \
	$(RESOLVE_STAGE); \
	echo "deploying stage=$$stage to $$AWS_REGION"; \
	cd infra && $(CDK) deploy --all --app '$(CDK_APP)' --context stage="$$stage" \
		$${POLICY_MODE:+--parameters "asdp-$$stage-gateway:PolicyEnforcementMode=$$POLICY_MODE"}
# POLICY_MODE flips Cedar enforcement as a DEPLOY so it lands in CloudTrail (§9.4):
#   POLICY_MODE=ENFORCE make deploy-dev
# Omitted, CDK reuses the stack's previous value — the flip is sticky, not per-deploy.

.PHONY: deploy
deploy: package ## ⚠️ Deploy the production-shaped stack. Human-only. (M10)
	@$(LOAD_ENV); \
	$(REQUIRE_REGION); \
	cd infra && $(CDK) deploy --all --app '$(CDK_APP)' --context stage=prod

.PHONY: destroy-dev
destroy-dev: ## ⚠️ Tear down the STAGE stack. Do this when you are done.
	@$(LOAD_ENV); \
	$(REQUIRE_REGION); \
	$(RESOLVE_STAGE); \
	echo "destroying stage=$$stage in $$AWS_REGION"; \
	cd infra && $(CDK) destroy --all --app '$(CDK_APP)' --context stage="$$stage"

# ─── Data — DEPLOYED ──────────────────────────────────────────────────────────
.PHONY: seed
seed: ## Write fabricated subjects into the deployed participants (M4)
	@# PII_ERASURE_TENANT is passed through and **checked against the seed file**, not
	@# used to override it. A hardcoded name three lines from a setting nothing reads is
	@# worse than no setting at all (V4-4) — but a setting that silently wins over the
	@# fixture is worse still, because it stamps one tenant's name onto another's data
	@# and every downstream count is wrong with no error to notice. Mismatch stops the run.
	@$(LOAD_ENV); \
	$(REQUIRE_REGION); \
	$(PY) -m pii_erasure.cli.main seed --tenant "$${PII_ERASURE_TENANT:-meridian}" 		$${ALLOW_SES_SANDBOX:+--allow-ses-sandbox}

.PHONY: inspect
inspect: ## Dump one participant's state. Usage: make inspect P=compliance-archive (M4)
	$(PY) -m pii_erasure.cli.main inspect --participant $(P)

# ─── Running — DEPLOYED ───────────────────────────────────────────────────────
# Every target below ran behind an `if <file exists>` guard printing "⏳ lands at Mx"
# until M9. The guards were correct while the files were unwritten and became a hazard
# the moment they were not: delete a suite and its gate printed a milestone note and
# EXITED 0. A gate that cannot fail is this repo's oldest defect class (VALIDATION
# baseline #2), so with every suite written they are gone — a missing file is now a
# missing file, loudly.
.PHONY: walkthrough
walkthrough: ## Full arc against the dev stack: discover → soft → pause → hard → cert (M8)
	@# .env carries PII_ERASURE_OPERATOR_USER/PASSWORD, and approval has no bypass — so
	@# without LOAD_ENV an operator who set them correctly is told they set nothing. This
	@# is the same inertness the LOAD_ENV comment above describes; the M8 targets were
	@# written without it and would have re-earned that finding on the deployed gate.
	@$(LOAD_ENV); 	$(REQUIRE_REGION); 	$(PY) -m pii_erasure.cli.main walkthrough

.PHONY: discover
discover: ## Discover one subject. Usage: make discover SUBJECT=sub_7f3a (M7)
	@$(LOAD_ENV); 	$(REQUIRE_REGION); 	$(PY) -m pii_erasure.cli.main discover --subject $(SUBJECT)

.PHONY: threads
threads: ## List checkpoint threads and their paused state — nothing is running (M8)
	@$(LOAD_ENV); 	$(REQUIRE_REGION); 	$(PY) -m pii_erasure.cli.main threads --list

.PHONY: approve
approve: ## Approve or deny. Usage: make approve THREAD=saga_01JQ8 DECISION=approve (M8)
	@$(LOAD_ENV); 	$(REQUIRE_REGION); 	$(PY) -m pii_erasure.cli.main approve --thread $(THREAD) --decision $(DECISION)

.PHONY: resume
resume: ## Manually resume a paused saga. Usage: make resume THREAD=saga_01JQ8 (M8)
	@$(LOAD_ENV); 	$(REQUIRE_REGION); 	$(PY) -m pii_erasure.cli.main resume --thread $(THREAD)

.PHONY: ledger
ledger: ## Print the hash-chained audit ledger and verify the chain (M5)
	@$(LOAD_ENV); 	$(REQUIRE_REGION); 	$(PY) -m pii_erasure.cli.main ledger --verify

# ─── Gates — DEPLOYED ─────────────────────────────────────────────────────────
.PHONY: conformance
conformance: package synth ## 5 verbs x 8 participants, against the deployed stack (M2)
	@# `package synth` are prerequisites, not politeness: the suite's preflight compares
	@# the asset hash CDK derives from the working tree against the one the deployed
	@# stack is running, and that comparison is only meaningful if both sides are
	@# current. Synthesising from a stale staging directory produces a hash that matches
	@# the deployed stack and reports "up to date" while testing yesterday's bytes —
	@# which is exactly how V7-2 wasted a deploy-and-test cycle.
	$(PYTEST) tests/conformance -m conformance

.PHONY: integration
integration: package synth ## Deployed: manifest signing vs the real CMK (M3) + full saga (M5)
	@# `package synth` for the same reason as conformance: the suite's preflight
	@# compares working-tree asset hashes against the deployed participants AND saga
	@# stacks, and that comparison is only meaningful if both sides are current (V7-2).
	$(PYTEST) tests/integration -m integration

.PHONY: chaos
chaos: ## Participant failures, duplicate wakes, resurrection at T+7 (M9)
	$(PYTEST) tests/integration -m chaos

.PHONY: eval
eval: ## Discovery recall vs generated ground truth. Gate: recall == 1.0 (M7)
	$(PY) -m evals.run --suite discovery --fail-under-recall 1.0

.PHONY: eval-adversarial
eval-adversarial: ## Injection corpus. Pass = tool absent or policy denied. (M7)
	$(PY) -m evals.run --suite adversarial

# ─── Release gates (ADR-016) ──────────────────────────────────────────────────
.PHONY: upgrade-canary
upgrade-canary: ## REQUIRED before bumping langgraph OR langgraph-checkpoint-aws (M9)
	bash scripts/upgrade_canary.sh

# ─── Misc ─────────────────────────────────────────────────────────────────────
.PHONY: diagrams
diagrams: ## Render docs/diagrams/*.mermaid to SVG
	@mkdir -p docs/diagrams/out
	@for f in docs/diagrams/*.mermaid; do \
		npx -y @mermaid-js/mermaid-cli -i $$f -o docs/diagrams/out/$$(basename $$f .mermaid).svg -b transparent; \
	done

.PHONY: clean
clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build .coverage htmlcov cdk.out
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
