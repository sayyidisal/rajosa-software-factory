.PHONY: dev-api dev-web test-api

# Loads apps/api/.env when present. The API also loads the same file in Python
# (without overriding variables already set in the process environment).
dev-api:
	@if [ -f apps/api/.env ]; then set -a; . ./apps/api/.env; set +a; fi; \
	echo "Starting API (AGENT_ADAPTER=$${AGENT_ADAPTER:-mock})"; \
	apps/api/.venv/bin/uvicorn app.main:app --app-dir apps/api --host 127.0.0.1 --port 8000 --reload

dev-web:
	npm --prefix apps/web run dev -- --hostname 127.0.0.1 --port 3000

test-api:
	cd apps/api && .venv/bin/pytest
