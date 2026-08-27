.PHONY: reset install-dev install-pre-commit-hook tests typecheck lint build smoke-test

reset:
	rm -rf .venv

install-pre-commit-hook:
	.venv/bin/pre-commit install --install-hooks

install-dev:
	devenv sync
	uv sync --group dev
	$(MAKE) install-pre-commit-hook

install-ci:
	which uv || (curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh)
	uv sync --group dev

tests:
	pytest -vv tests

typecheck:
	mypy --strict sentry_taskbroker_management
	mypy --strict tests

lint:
	black --config=pyproject.toml sentry_taskbroker_management
	flake8 sentry_taskbroker_management
	isort sentry_taskbroker_management

build:
	uv build --wheel

# Cluster-backed smoke test; needs a local kube-context (see smoke-test/README.md).
smoke-test:
	./smoke-test/run.sh
