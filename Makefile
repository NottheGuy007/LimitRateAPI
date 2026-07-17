.PHONY: help venv install dev test run clean

PYTHON ?= python3
VENV ?= .venv

help:
	@echo "keepalive-api targets:"
	@echo "  make venv        - create a virtualenv in $(VENV)"
	@echo "  make install     - install runtime deps (needs venv)"
	@echo "  make dev         - install runtime + dev/test deps"
	@echo "  make test        - run the test suite"
	@echo "  make run         - run the gateway with config.yaml"
	@echo "  make example     - run with config.example.yaml"
	@echo "  make clean       - remove caches and venv"

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install -r requirements.txt

dev: venv
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install -r requirements.txt
	$(VENV)/bin/pip install pytest pytest-asyncio respx

test:
	$(VENV)/bin/python -m pytest -v

run:
	$(VENV)/bin/python -m app.main

example:
	KEEPALIVE_CONFIG=config.example.yaml $(VENV)/bin/python -m app.main

clean:
	rm -rf $(VENV) .pytest_cache __pycache__ */__pycache__ */*/__pycache__
