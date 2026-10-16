.PHONY: dev-api dev-web test-api

# Supported topology: exactly one API process per SQLite DB/workspace.
# Uses python -m app.run_dev (single process + --reload). Do not add --workers.
# Loads apps/api/.env when present. The API also loads the same file in Python
# (without overriding variables already set in the process environment).
dev-api:
	@if [ -f apps/api/.env ]; then set -a; . ./apps/api/.env; set +a; fi; \
	echo "Starting API (AGENT_ADAPTER=$${AGENT_ADAPTER:-mock}, single-process)"; \
	cd apps/api && .venv/bin/python -m app.run_dev

dev-web:
	npm --prefix apps/web run dev -- --hostname 127.0.0.1 --port 3000

test-api:
	cd apps/api && .venv/bin/pytest
