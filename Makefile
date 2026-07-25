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

# src and tests exist from commit zero; evals and seeds land at M4/M7. Lint only
# the dirs that exist so `make check` is green at commit zero, and pick them up
# automatically the moment they appear — same "becomes mandatory when it lands"
# rule the milestone gates use (docs/ROADMAP.md, rule 4). Not a silencing guard.
LINT_DIRS := src tests $(wildcard evals seeds)

# Milestone-gated targets: stages that haven't been built yet print "⏳ lands
# at Mx" instead of failing, so `make check` and CI are green from commit zero.
# The guard is the *existence of the stage's entry file* — the moment a
# milestone lands, its gate becomes mandatory automatically. Never re-add a
# guard to silence a failing gate (docs/ROADMAP.md, rule 4).

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ─── Setup ────────────────────────────────────────────────────────────────────
.PHONY: install
install: ## Create venv and install with dev extras
	python3 -m venv .venv
	$(PIP) install -U pip
	$(PIP) install -e ".[dev,otel]"
	@test -f .env || cp .env.example .env
	@echo "✅ next: make check · then open docs/ROADMAP.md"

# ─── Fake data (M4) ───────────────────────────────────────────────────────────
.PHONY: seed
seed: ## Seed the 8 fake subsystems with made-up subjects
	$(PY) -m pii_erasure.cli.main seed --tenant meridian

.PHONY: reset
reset: ## Wipe all local state and reseed
	rm -rf .state && $(MAKE) seed

# ─── Running ──────────────────────────────────────────────────────────────────
.PHONY: demo
demo: ## Full walkthrough with real Bedrock discovery (needs AWS credentials) (M10)
	PII_ERASURE_OFFLINE=0 $(PY) -m pii_erasure.cli.main demo

.PHONY: demo-offline
demo-offline: ## Same walkthrough, stub model + SQLite. No AWS, no cost. (M8)
	PII_ERASURE_OFFLINE=1 $(PY) -m pii_erasure.cli.main demo

.PHONY: discover
discover: ## Discover one subject. Usage: make discover SUBJECT=sub_7f3a (M7)
	$(PY) -m pii_erasure.cli.main discover --subject $(SUBJECT)

.PHONY: inspect
inspect: ## Dump a participant's raw fake data. Usage: make inspect P=aegis-archive (M4)
	$(PY) -m pii_erasure.cli.main inspect --participant $(P)

.PHONY: threads
threads: ## List LangGraph checkpoint threads and their paused state (M8)
	$(PY) -m pii_erasure.cli.main threads --list

.PHONY: resume
resume: ## Resume a paused saga. Usage: make resume THREAD=saga_01JQ8 DECISION=approve (M8)
	$(PY) -m pii_erasure.cli.main resume --thread $(THREAD) --decision $(DECISION)

.PHONY: ledger
ledger: ## Print the hash-chained audit ledger and verify the chain (M5)
	$(PY) -m pii_erasure.cli.main ledger --verify

# ─── Quality ──────────────────────────────────────────────────────────────────
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
test: ## Unit tests
	$(PYTEST) tests/unit

.PHONY: conformance
conformance: ## Every participant must pass the 5-verb contract suite (M2)
	@if ls tests/conformance/test_*.py >/dev/null 2>&1; then \
		$(PYTEST) tests/conformance -m conformance; \
	else echo "⏳ lands at M2 — docs/ROADMAP.md"; fi

.PHONY: integration
integration: ## Full saga, all phases, against the fake subsystems (M5)
	@if ls tests/integration/test_*.py >/dev/null 2>&1; then \
		$(PYTEST) tests/integration -m integration; \
	else echo "⏳ lands at M5 — docs/ROADMAP.md"; fi

.PHONY: policy-test
policy-test: ## Cedar policy unit tests (M6)
	@if [ -f tests/unit/test_policies.py ]; then \
		$(PYTEST) tests/unit/test_policies.py; \
	else echo "⏳ lands at M6 — docs/ROADMAP.md"; fi

# ─── Evaluation ───────────────────────────────────────────────────────────────
.PHONY: eval
eval: ## Discovery recall vs generated ground truth. Gate: recall == 1.0 (M7)
	@if [ -f evals/run.py ]; then \
		$(PY) -m evals.run --suite discovery --fail-under-recall 1.0; \
	else echo "⏳ lands at M7 — docs/ROADMAP.md"; fi

.PHONY: eval-adversarial
eval-adversarial: ## Injection corpus. Pass = policy denied, not model resisted. (M7)
	@if [ -f evals/run.py ]; then \
		$(PY) -m evals.run --suite adversarial; \
	else echo "⏳ lands at M7 — docs/ROADMAP.md"; fi

# ─── Release gates (ADR-014) ──────────────────────────────────────────────────
.PHONY: upgrade-canary
upgrade-canary: ## REQUIRED before any langgraph bump. Pause, upgrade, resume. (M9)
	@if [ -f tests/integration/test_upgrade_canary.py ]; then \
		bash scripts/upgrade_canary.sh; \
	else echo "⏳ lands at M9 — docs/ROADMAP.md"; fi

# ─── Infrastructure (M10, optional) ───────────────────────────────────────────
.PHONY: synth
synth: ## CDK synth: Aurora checkpointer, Fargate, EventBridge Scheduler
	cd infra && npx -y aws-cdk@2 synth

.PHONY: deploy
deploy: ## Deploy to AWS. Costs money. Human-only — read infra/README.md first.
	cd infra && npx -y aws-cdk@2 deploy --all

# ─── Everything ───────────────────────────────────────────────────────────────
.PHONY: check
check: lint test conformance policy-test ## What CI runs on every PR

.PHONY: diagrams
diagrams: ## Render docs/diagrams/*.mermaid to SVG
	@mkdir -p docs/diagrams/out
	@for f in docs/diagrams/*.mermaid; do \
		npx -y @mermaid-js/mermaid-cli -i $$f -o docs/diagrams/out/$$(basename $$f .mermaid).svg -b transparent; \
	done

.PHONY: clean
clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
