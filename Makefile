.DEFAULT_GOAL := help

## Compile requirements (application, then development groups)
.PHONY: reqs
reqs:
	uv pip compile pyproject.toml -o requirements.txt $(ARGS)
	uv pip compile pyproject.toml -o requirements-dev.txt --group dev --group docs -c requirements.txt $(ARGS)

## Upgrade requirements
.PHONY: upgrade
upgrade: ARGS = --upgrade
upgrade: reqs

## Install development dependencies
.PHONY: deps
deps:
	pip install -r requirements-dev.txt && pip install -e . --no-deps

## Set up a checkout for development: dependencies, package and git hooks
.PHONY: init
init: deps
	pre-commit install

## Build the documentation into docs/_build/html/
.PHONY: docs
docs:
	sphinx-build -b html docs docs/_build/html -W --keep-going

## Increment git tag
.PHONY: tag
tag:
	./scripts/increment-git-tag.sh

## Delete compiled Python files and caches
.PHONY: clean
clean:
	find . -type f -name "*.py[co]" -delete
	find . -type d -name "__pycache__" -delete
	rm -rf .pytest_cache
	rm -rf .ruff_cache
	rm -rf ./src/*.egg-info
	rm -rf docs/_build

## Show this help message
.PHONY: help
help:
	@awk '\
		/^##/ {sub(/^## ?/, "", $$0); doc=$$0; next} \
		/^[a-zA-Z0-9_.-]+:/ && $$1 !~ /^\./ { \
			target=$$1; sub(/:.*/, "", target); \
			print target "|" doc; doc="" \
		} \
	' $(MAKEFILE_LIST) | sort | awk -F"|" '{printf "%-20s %s\n", $$1, $$2}'
