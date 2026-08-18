.PHONY: test test-unit test-workflow test-contract test-flow test-integration test-e2e test-pr coverage lint

PYTEST := uv run pytest

test: test-unit test-workflow test-contract test-flow

test-unit:
	$(PYTEST) tests/unit -q

test-workflow:
	$(PYTEST) tests/workflow -q

test-contract:
	$(PYTEST) tests/contracts -q

test-flow:
	$(PYTEST) tests/flows -q

test-integration:
	$(PYTEST) tests/integration -q --strict-markers -m "integration and not quarantine"

test-e2e:
	$(PYTEST) tests/e2e -q --strict-markers -m "e2e and not external"

test-pr:
	$(PYTEST) tests/unit tests/workflow tests/contracts tests/flows -q

coverage:
	$(PYTEST) tests/unit tests/workflow tests/contracts tests/flows --strict-markers --cov=forge --cov-branch --cov-report=term-missing --cov-report=xml

lint:
	uv run ruff check src
	uv run ruff format --check src
