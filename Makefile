PYTHON ?= .venv/bin/python
UV ?= uv
MODULE := code_symbol_index
PACKAGE := code-symbol-index
VERSION := $(shell $(PYTHON) -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')

.PHONY: help venv install test check smoke clean build publish-check publish

help:
	@printf '%s\n' \
		'Targets:' \
		'  make install        Install editable package with dev dependencies' \
		'  make test           Run tests' \
		'  make check          Run syntax check and tests' \
		'  make smoke          Run a small CLI smoke test' \
		'  make build          Build package artifacts' \
		'  make publish-check  Dry-run publish built artifacts' \
		'  make publish        Publish built artifacts' \
		'  make clean          Remove local build/test/index artifacts'

venv:
	$(UV) venv .venv

install: venv
	$(UV) pip install --python $(PYTHON) -e '.[dev]'

test:
	$(PYTHON) -m pytest -q

check:
	$(PYTHON) -m py_compile $(MODULE).py
	$(PYTHON) -m pytest -q

smoke:
	tmp=$$(mktemp -d); \
	trap 'rm -rf "$$tmp"' EXIT; \
	printf 'class Tool:\n    pass\n' > "$$tmp/app.py"; \
	$(PYTHON) $(MODULE).py index --root "$$tmp" --language python >/dev/null; \
	$(PYTHON) $(MODULE).py search Tool --root "$$tmp" --language python >/dev/null; \
	$(PYTHON) $(MODULE).py status --root "$$tmp" >/dev/null

clean:
	rm -rf .pytest_cache __pycache__ tests/__pycache__
	rm -rf .code-symbol-index build dist *.egg-info

build: clean
	$(UV) build

publish-check: build
	$(UV) publish --dry-run dist/*

publish: build
	$(UV) publish dist/*
