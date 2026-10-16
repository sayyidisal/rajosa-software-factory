# Local smoke API harness (Phase 1A)

Dedicated launcher for an isolated local API process using the **official**
entry point (`python -m app.run_dev`). It does **not** change `make dev-api`
defaults.

## What it does

1. Creates a unique disposable directory under `/tmp` (`rajosa-smoke.*`).
2. Optionally sources `apps/api/.env` for ordinary host settings (same pattern as `dev-api`).
3. Forces smoke overrides afterward (these win over anything inherited from `.env`):
   - absolute disposable `TASK_DB_PATH` and `WORKSPACE_ROOT` under that directory
   - `AGENT_ADAPTER=mock`
   - `EXECUTION_RUNNER=off`
   - `API_HOST=127.0.0.1` (loopback only; non-loopback values are refused by preflight)
   - a smoke-only `RAJOSA_API_TOKEN` generated in the recipe environment (never printed or written to a file)
   - default `API_PORT=18000` (override with `SMOKE_API_PORT` if needed)
4. Runs `python -m app.smoke_preflight` (fail-closed), which validates **effective**
   settings from `get_settings()` (process env plus non-overriding `.env` load) —
   including disposable paths, mock adapter, runner off, usable token, and loopback bind.
5. Starts `python -m app.run_dev` only if preflight succeeds.

## Run (Phase 1A — server only)

```bash
make smoke-api
```

Stop with Ctrl-C. The disposable root is **kept** after the server has started so child `--reload` processes are not racing a delete. Remove it manually when finished:

```bash
rm -rf /tmp/rajosa-smoke.*
```

(Only remove directories you created for smoke; do not delete `apps/api/var/rajosa.sqlite3`.)

## Run (Phase 1B — HTTP create/approve smoke)

Starts the official `python -m app.run_dev` entry under disposable config, verifies
health / auth / create / approve on loopback, then stops the process group.

```bash
make smoke-api-http
```

Optional: `SMOKE_API_PORT=18001 make smoke-api-http`
Optional retain root: `SMOKE_KEEP_ROOT=1 make smoke-api-http`

Does **not** call `/tasks/{id}/execute-tests`. Does **not** enable Docker/SumoPod.

## Safety guarantees

- Preflight refuses the active DB (`apps/api/var/rajosa.sqlite3`) and active workspace (`apps/api/var/workspaces`).
- Preflight refuses paths outside the per-run disposable root.
- Preflight refuses non-mock adapters and any runner other than `off`.
- Preflight refuses missing/short API tokens (fail-closed auth unchanged).
- Preflight refuses non-loopback `API_HOST` (only `127.0.0.1`, `localhost`, `::1`).
- If preflight fails, the temp root is removed and Uvicorn is not started.

## Cleanup

| Situation | Temp root |
|---|---|
| Preflight fails (1A or 1B) | Removed automatically |
| `make smoke-api` after server start | Kept — delete manually when idle |
| `make smoke-api-http` success + confirmed stop | Removed automatically |
| `make smoke-api-http` failure or unconfirmed kill | Retained; path printed in notes (no token) |
