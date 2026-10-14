.DEFAULT_GOAL := help
UV ?= uv
TENANT ?= cu-alpha

.PHONY: help setup mock lint typecheck test test-unit test-e2e check schemas \
        discover discover-offline replay replay-beta demo-errors demo-handoff evidence clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies and the Chromium build Playwright drives
	$(UV) sync
	$(UV) run playwright install chromium
	@test -f .env || cp .env.example .env

mock: ## Run the mock legacy core (cu-alpha :8401, cu-beta :8402)
	$(UV) run mock-corebank serve

lint: ## ruff lint + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck: ## mypy --strict
	$(UV) run mypy src tests scripts

test-unit: ## Fast unit tests (no browser)
	$(UV) run pytest tests/unit -q

test-e2e: ## Browser end-to-end tests (starts its own mock core)
	$(UV) run pytest tests/integration -q

test: ## All tests
	$(UV) run pytest -q

check: lint typecheck test ## Everything CI runs

schemas: ## Export JSON Schemas of all artifact kinds to schemas/
	$(UV) run cua schema --out schemas

discover: ## Real LLM discovery of the balance lookup (needs an API key in .env and `make mock`)
	$(UV) run cua discover requests/get_savings_balance.yaml --tenant $(TENANT)

discover-offline: ## Same pipeline with the reference plan instead of an LLM
	$(UV) run cua discover requests/get_savings_balance.yaml --tenant $(TENANT) --provider scripted

replay: ## Deterministic replay (no LLM)
	$(UV) run cua replay corebank.member.get_savings_balance -t cu-alpha -p member_id=100234

replay-beta: ## Same capability on the differently-configured tenant
	$(UV) run cua replay corebank.member.get_savings_balance -t cu-beta -p member_id=200145

demo-errors: ## Business outcomes, recoveries and a hard failure, back to back
	-$(UV) run cua replay corebank.member.get_savings_balance -p member_id=999999
	-$(UV) run cua replay corebank.member.get_savings_balance -p member_id=12AB
	$(UV) run mock-corebank fault cu-alpha maintenance_notice --path mbr_detail
	-$(UV) run cua replay corebank.member.get_savings_balance -p member_id=100234
	$(UV) run mock-corebank fault cu-alpha server_error --path mbr_detail --count 5
	-$(UV) run cua replay corebank.member.get_savings_balance -p member_id=100234
	$(UV) run mock-corebank clear cu-alpha

demo-handoff: ## Unknown modal -> escalate -> operator clears it in the live session -> resume
	$(UV) run mock-corebank fault cu-alpha unknown_dialog --path mbr_search
	$(UV) run python scripts/operator_bot.py --kind failure --click 'button "Continue Session"' \
		--resolution retry_step --note "Dismissed duplicate-session prompt" & \
	$(UV) run cua replay corebank.member.get_savings_balance -p member_id=100234 --on-failure escalate

evidence: ## Regenerate evidence/ (all scenarios; add ARGS=--discover for LLM discovery first)
	$(UV) run python scripts/collect_evidence.py $(ARGS)

clean: ## Remove local run output and caches
	rm -rf runs .pytest_cache .mypy_cache .ruff_cache
