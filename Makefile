PY_PATHS = src tests scripts

.PHONY: help install format format-check lint lint-fix typecheck test clean \
	verify run run-http token ui-install ui-build ui-typecheck ui-test check-wheel dist

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install the package and the dev tools
	uv sync

format: ## Format code with ruff
	uv run ruff format $(PY_PATHS)

format-check: ## Check code formatting with ruff
	uv run ruff format --check $(PY_PATHS)

lint: ## Lint code with ruff
	uv run ruff check $(PY_PATHS)

lint-fix: ## Lint and fix code with ruff
	uv run ruff check --fix $(PY_PATHS)

typecheck: ## Type check with ty
	uv run ty check $(PY_PATHS)

test: ui-build ## Run every Python suite (the wheel test needs the built UI)
	uv run pytest -v


check-wheel: ui-build ## Build the wheel and check it carries the UI, the skill and the migrations
	@rm -rf dist
	@uv build --wheel --out-dir dist >/dev/null
	@uv run python scripts/check_wheel.py dist
	@rm -rf dist

dist: ui-build ## Build the sdist and wheel into dist/ (the wheel is built from the sdist)
	@rm -rf dist
	uv build --out-dir dist
	uv run python scripts/check_wheel.py dist

verify: format-check lint typecheck ui-build test check-wheel ui-typecheck ui-test ## Every check CI runs

run: ## Serve the local runtime over stdio
	uv run todosforagents

run-http: ## Serve the local runtime over HTTP on 127.0.0.1:$$TODOS_LOCAL_PORT (default 8765)
	uv run todosforagents --http

token: ## Print the bearer token local HTTP requires
	uv run todosforagents --print-token

ui-install: ## Install the UI's dependencies from its lock file
	cd ui && npm ci

ui-build: ui-install ## Build the UI to ui/dist/index.html, which the wheel carries
	cd ui && npm run build

ui-typecheck: ui-install ## Typecheck the UI (the vite build strips types without checking them)
	cd ui && npx tsc --noEmit

ui-test: ui-install ## Run the UI's vitest suite
	cd ui && npm test

clean: ## Remove build output and caches
	find . -type d -name "__pycache__" -not -path "./ui/node_modules/*" -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist/ ui/dist/ .pytest_cache .ruff_cache *.mcpb
