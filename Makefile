.PHONY: install test lint up down logs venv

# Prefer a project-local venv if present (created via `make venv`). Falls back
# to system python3 otherwise — only works when system python is >= 3.11.
PY := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

venv:
	/usr/local/bin/python3.11 -m venv .venv || python3.11 -m venv .venv
	@echo "venv ready — future 'make install' / 'make test' will use .venv/bin/python"

install:
	$(PY) -m pip install -e shared/veilwatch_contracts
	$(PY) -m pip install -e services/capture
	$(PY) -m pip install -e services/detection
	$(PY) -m pip install -e 'services/confirmation[test]'
	$(PY) -m pip install -e 'services/tracker[test]'
	$(PY) -m pip install pytest

test:
	$(PY) -m pytest tests -v

up:
	docker compose up -d kafka redis postgres minio
	docker compose up --build capture detection tracker confirmation

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=100 capture detection confirmation
