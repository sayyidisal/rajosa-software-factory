.PHONY: dev-api dev-web test-api smoke-api smoke-api-http

# Supported topology: exactly one API process per SQLite DB/workspace.
# Uses python -m app.run_dev (single process + --reload). Do not add --workers.
# Loads apps/api/.env when present. The API also loads the same file in Python
# (without overriding variables already set in the process environment).
dev-api:
	@if [ -f apps/api/.env ]; then set -a; . ./apps/api/.env; set +a; fi; \
	echo "Starting API (AGENT_ADAPTER=$${AGENT_ADAPTER:-mock}, single-process)"; \
	cd apps/api && .venv/bin/python -m app.run_dev

# Phase 1A — disposable local smoke harness (official run_dev entry; no HTTP).
# Sources .env for ordinary host settings, then forces smoke overrides.
# Does not modify developer defaults for `make dev-api`.
# Temp root is removed only if the server never started (preflight failure).
smoke-api:
	@set -eu; \
	SMOKE_ROOT=$$(mktemp -d "${TMPDIR:-/tmp}/rajosa-smoke.XXXXXX"); \
	STARTED=0; \
	cleanup() { \
	  if [ "$$STARTED" = "0" ]; then rm -rf "$$SMOKE_ROOT"; fi; \
	}; \
	trap cleanup EXIT; \
	if [ -f apps/api/.env ]; then set -a; . ./apps/api/.env; set +a; fi; \
	SMOKE_TOKEN=$$(cd apps/api && .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'); \
	export SMOKE_ROOT; \
	export TASK_DB_PATH="$$SMOKE_ROOT/smoke.sqlite3"; \
	export WORKSPACE_ROOT="$$SMOKE_ROOT/workspaces"; \
	export AGENT_ADAPTER=mock; \
	export EXECUTION_RUNNER=off; \
	export RAJOSA_API_TOKEN="$$SMOKE_TOKEN"; \
	export API_HOST=127.0.0.1; \
	export API_PORT="$${SMOKE_API_PORT:-18000}"; \
	unset SMOKE_TOKEN; \
	mkdir -p "$$WORKSPACE_ROOT"; \
	echo "Starting smoke API (mock, runner=off, disposable root under /tmp; port $$API_PORT)"; \
	echo "Smoke root kept on server exit for inspection: $$SMOKE_ROOT"; \
	cd apps/api && .venv/bin/python -m app.smoke_preflight; \
	STARTED=1; \
	.venv/bin/python -m app.run_dev

# Phase 1B — start disposable API, run HTTP create/approve smoke, stop process.
# Reuses Phase 1A preflight overrides; does not call execute-tests or enable runner.
smoke-api-http:
	@cd apps/api && .venv/bin/python -m app.smoke_http

dev-web:
	npm --prefix apps/web run dev -- --hostname 127.0.0.1 --port 3000

test-api:
	cd apps/api && .venv/bin/pytest
