# Everything you run, from the repository root.
#   make help
#
# The product lives in code/ (the wyld3rness harness, with FindyFixy under
# code/examples/findyfixy/) and has its own Makefile. The targets here either
# hand off to it or run the repository checks in tools/. Nothing here does work of
# its own, so the real commands stay readable in one place each.

.DEFAULT_GOAL := help
.PHONY: help adr-snapshot setup up down sim-up load-test controls-pilot controls oncall oncall-up injection experiment experiment-power experiment-analyze console triage test test-harness lint eval-retrieval eval eval-gate eval-baseline eval-freeze eval-compare eval-negative obs-secrets obs-up obs-down obs-prices case golden ingest audit verify-attestation gate-at-commit gate-at-push check

help:
	@echo "First time:       make setup"
	@echo "Start / stop:     make up (everything but Ollama)   make down"
	@echo "Simulated prod:   make sim-up   make load-test   make controls-pilot   make controls   (inspect on 127.0.0.1:8100)"
	@echo "On call:          make oncall-up   make oncall   (agent and loop in containers; storms, no person in it)   make injection   make experiment"
	@echo "Run the product:  make console   make triage   make ingest"
	@echo "Measure / test:   make test   make test-harness   make lint   make eval-retrieval"
	@echo "Answer quality:   make eval   make eval-gate   make eval-baseline   make eval-negative"
	@echo "CI gate:          make verify-attestation   (the attestation eval-gate writes on a pass)"
	@echo "Gate at commit:   make gate-at-commit   make gate-at-push   (run by the git hooks)"
	@echo "See test cases:   make golden   make case ID=g07"
	@echo "Freeze baseline:  make eval-freeze REPORT=examples/findyfixy/reports/eval-<timestamp>.json"
	@echo "Compare runs:     make eval-compare REPORT=examples/findyfixy/reports/eval-<timestamp>.json"
	@echo "Trace backend:    make obs-secrets   make obs-up   make obs-down   make obs-prices   (Langfuse on 127.0.0.1:3000)"
	@echo "Decision records:  make adr-snapshot   (redraw the diagram in each ADR)"
	@echo "Before a commit:  make check   make audit"

# Product targets: see code/Makefile for what each one runs.
setup up down sim-up load-test controls-pilot controls oncall oncall-up injection experiment experiment-power console triage test test-harness lint eval-retrieval eval eval-gate eval-baseline eval-negative golden ingest audit verify-attestation gate-at-commit gate-at-push:
	$(MAKE) -C code $@

# Targets that take a value are forwarded explicitly, because the pattern rule
# above cannot pass variables to the code Makefile.
case:
	$(MAKE) -C code case ID=$(ID) REPORT=$(REPORT)

eval-freeze:
	$(MAKE) -C code eval-freeze REPORT=$(REPORT)

experiment-analyze:
	$(MAKE) -C code experiment-analyze FILE=$(FILE)

eval-compare:
	$(MAKE) -C code eval-compare REPORT=$(REPORT) OTHER=$(OTHER)

# One architecture picture per phase for the ADRs in code/docs/adr/, drawn from the live page.
adr-snapshot:
	python3 tools/adr_snapshot.py
	python3 tools/diagram.py

# check: the documentation drift check, the repository tools' tests and the
# privacy scan of the tree.
#
# The test tools come from uv's local cache (--offline), so a flaky connection
# never refuses a commit with nothing wrong in it. A probe fetches them online
# only when the cache lacks them (a fresh clone or machine); the tests then run
# once and their exit code passes through unchanged.
UV_TOOLS = uv run --no-project
check:
	python3 tools/check_docs.py
	@$(UV_TOOLS) --offline --with pytest true 2>/dev/null || { echo "(test tools not cached, fetching them online)"; $(UV_TOOLS) --with pytest true; }
	@$(UV_TOOLS) --offline --with pytest pytest tools -q
	python3 tools/scan_private.py --tree

obs-secrets obs-up obs-down obs-prices:
	$(MAKE) -C code $@
