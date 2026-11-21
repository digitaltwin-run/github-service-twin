PYTHON ?= python3
PORT ?= 8796
STATE_DIR ?= .twin-state

.PHONY: test lint serve demo clean

test:
	PYTHONPATH=src $(PYTHON) -m pytest -q

lint:
	ruff check src tests

serve:
	PYTHONPATH=src $(PYTHON) -m github_service_twin.cli serve --host 127.0.0.1 --port $(PORT) --state-dir $(STATE_DIR)

demo:
	PYTHONPATH=src $(PYTHON) -m github_service_twin.cli demo

clean:
	rm -rf $(STATE_DIR) .pytest_cache .ruff_cache __pycache__
