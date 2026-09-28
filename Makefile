## Compile requirements (application, then development)
.PHONY: reqs
reqs:
	uv pip compile pyproject.toml -o requirements.txt
	uv pip compile pyproject.toml -o requirements-dev.txt --all-extras

## Upgrade requirements
.PHONY: upgrade
upgrade:
	uv pip compile pyproject.toml -o requirements.txt --upgrade
	uv pip compile pyproject.toml -o requirements-dev.txt --all-extras --upgrade

## Install development dependencies
.PHONY: deps
deps:
	pip install -r requirements-dev.txt && pip install -e .[dev]

## Set up a checkout for development: dependencies, package and git hooks
.PHONY: init
init: deps
	pre-commit install

## Generate API documentation
.PHONY: docs
docs:
	pdoc --docformat google -o docs/api/ curated_feed

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
