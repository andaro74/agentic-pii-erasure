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

# src and tests exist from commit zero; infra lands at M0, evals and seeds at
# M4/M7. Lint only the dirs that exist so `make check` is green at commit zero,
# and pick them up automatically the moment they appear — the same "becomes
# mandatory when it lands" rule the milestone gates use (docs/ROADMAP.md, rule 4).
# Not a silencing guard.
LINT_DIRS := src tests $(wildcard infra evals seeds)

# Milestone-gated targets: stages that haven't been built yet print "⏳ lands
# at Mx" instead of failing, so `make check` and CI are green from commit zero.
# The guard is the *existence of the stage's entry file* — the moment a
# milestone lands, its gate becomes mandatory automatically. Never re-add a
# guard to silence a failing gate (docs/ROADMAP.md, rule 4).
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
		$(PY) -m pip install -c requirements.lock -e ".[dev,infra,otel]"; \
	else \
		$(PY) -m pip install -e ".[dev,infra,otel]"; \
	fi
	@test -f .env || cp .env.example .env
	@echo "✅ next: make check · then open docs/ROADMAP.md"

.PHONY: lock
lock: ## Regenerate requirements.lock from the runtime deps (invariant 9 mechanism)
	$(PY) -m pip install -q pip-tools
	$(PY) -m piptools compile --quiet --strip-extras -o requirements.lock pyproject.toml
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
	@if [ -f tests/unit/test_policies.py ]; then \
		$(PYTEST) tests/unit/test_policies.py; \
	else echo "⏳ lands at M6 — docs/ROADMAP.md"; fi

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
	@if [ -f infra/app.py ]; then \
		cd infra && $(CDK) synth --quiet --app '$(CDK_APP)'; \
	else echo "⏳ lands at M0 — docs/ROADMAP.md"; fi

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
LAMBDA_PLATFORM := manylinux2014_x86_64
LAMBDA_PY := 3.12

.PHONY: package
package: ## Stage the participant Lambda asset (handler code + deps). Runs before deploy.
	@rm -rf $(LAMBDA_ASSET) && mkdir -p $(LAMBDA_ASSET)
	$(PY) -m pip install --quiet --target $(LAMBDA_ASSET) \
		--platform $(LAMBDA_PLATFORM) --python-version $(LAMBDA_PY) \
		--implementation cp --only-binary=:all: \
		"pydantic>=2.9,<3" "structlog>=24.1"
	@cp -r src/pii_erasure $(LAMBDA_ASSET)/
	@find $(LAMBDA_ASSET) -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ staged $(LAMBDA_ASSET) ($$(du -sh $(LAMBDA_ASSET) | cut -f1))"

.PHONY: bootstrap
bootstrap: ## ⚠️ ONE-TIME per account+region: create the CDK toolkit stack. Human-only.
	@$(LOAD_ENV); \
	$(REQUIRE_REGION); \
	acct=$$(aws sts get-caller-identity --query Account --output text) || \
		{ echo "❌ no usable AWS credentials — configure them first"; exit 1; }; \
	echo "bootstrapping aws://$$acct/$$AWS_REGION"; \
	$(CDK) bootstrap "aws://$$acct/$$AWS_REGION"

.PHONY: deploy-dev
deploy-dev: package ## ⚠️ Deploy a dev-shaped stack (STAGE=dev by default). Costs money.
	@$(LOAD_ENV); \
	$(REQUIRE_REGION); \
	$(RESOLVE_STAGE); \
	echo "deploying stage=$$stage to $$AWS_REGION"; \
	cd infra && $(CDK) deploy --all --app '$(CDK_APP)' --context stage="$$stage"

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
	@# The tenant comes from .env rather than a literal here: a hardcoded name three
	@# lines from a PII_ERASURE_TENANT that nothing read is worse than no setting at
	@# all — it looks configurable and silently is not (VALIDATION V4-4).
	@$(LOAD_ENV); \
	$(PY) -m pii_erasure.cli.main seed --tenant "$${PII_ERASURE_TENANT:-meridian}"

.PHONY: inspect
inspect: ## Dump one participant's state. Usage: make inspect P=compliance-archive (M4)
	$(PY) -m pii_erasure.cli.main inspect --participant $(P)

# ─── Running — DEPLOYED ───────────────────────────────────────────────────────
.PHONY: walkthrough
walkthrough: ## Full arc against the dev stack: discover → soft → pause → hard → cert (M8)
	$(PY) -m pii_erasure.cli.main walkthrough

.PHONY: discover
discover: ## Discover one subject. Usage: make discover SUBJECT=sub_7f3a (M7)
	$(PY) -m pii_erasure.cli.main discover --subject $(SUBJECT)

.PHONY: threads
threads: ## List checkpoint threads and their paused state — nothing is running (M8)
	$(PY) -m pii_erasure.cli.main threads --list

.PHONY: approve
approve: ## Approve or deny. Usage: make approve THREAD=saga_01JQ8 DECISION=approve (M8)
	$(PY) -m pii_erasure.cli.main approve --thread $(THREAD) --decision $(DECISION)

.PHONY: resume
resume: ## Manually resume a paused saga. Usage: make resume THREAD=saga_01JQ8 (M8)
	$(PY) -m pii_erasure.cli.main resume --thread $(THREAD)

.PHONY: ledger
ledger: ## Print the hash-chained audit ledger and verify the chain (M5)
	$(PY) -m pii_erasure.cli.main ledger --verify

# ─── Gates — DEPLOYED ─────────────────────────────────────────────────────────
.PHONY: conformance
conformance: ## 5 verbs x 8 participants, against the deployed stack (M2)
	@if ls tests/conformance/test_*.py >/dev/null 2>&1; then \
		$(PYTEST) tests/conformance -m conformance; \
	else echo "⏳ lands at M2 — docs/ROADMAP.md"; fi

.PHONY: integration
integration: ## Full three-phase saga against the deployed stack (M5)
	@if ls tests/integration/test_*.py >/dev/null 2>&1; then \
		$(PYTEST) tests/integration -m integration; \
	else echo "⏳ lands at M5 — docs/ROADMAP.md"; fi

.PHONY: chaos
chaos: ## Participant failures, duplicate wakes, resurrection at T+7 (M9)
	@if ls tests/integration/test_chaos*.py >/dev/null 2>&1; then \
		$(PYTEST) tests/integration -m chaos; \
	else echo "⏳ lands at M9 — docs/ROADMAP.md"; fi

.PHONY: eval
eval: ## Discovery recall vs generated ground truth. Gate: recall == 1.0 (M7)
	@if [ -f evals/run.py ]; then \
		$(PY) -m evals.run --suite discovery --fail-under-recall 1.0; \
	else echo "⏳ lands at M7 — docs/ROADMAP.md"; fi

.PHONY: eval-adversarial
eval-adversarial: ## Injection corpus. Pass = tool absent or policy denied. (M7)
	@if [ -f evals/run.py ]; then \
		$(PY) -m evals.run --suite adversarial; \
	else echo "⏳ lands at M7 — docs/ROADMAP.md"; fi

# ─── Release gates (ADR-016) ──────────────────────────────────────────────────
.PHONY: upgrade-canary
upgrade-canary: ## REQUIRED before bumping langgraph OR langgraph-checkpoint-aws (M9)
	@if [ -f tests/integration/test_upgrade_canary.py ]; then \
		bash scripts/upgrade_canary.sh; \
	else echo "⏳ lands at M9 — docs/ROADMAP.md"; fi

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
