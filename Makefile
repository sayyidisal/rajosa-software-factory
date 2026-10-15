.PHONY: dev-api dev-web test-api

dev-api:
	apps/api/.venv/bin/uvicorn app.main:app --app-dir apps/api --host 127.0.0.1 --port 8000 --reload

dev-web:
	npm --prefix apps/web run dev -- --hostname 127.0.0.1 --port 3000

test-api:
	cd apps/api && .venv/bin/pytest
