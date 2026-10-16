# Agent Orchestration — Design (4A) & Implementation (4B)

**Phase 4A status:** Design complete
**Phase 4B status:** Implemented (minimal deterministic orchestrator + mandatory plan approval)
**Date:** 2026-10-15
**Baseline commit (4A):** `5ec61e5`
**Related docs:** [architecture.md](./architecture.md), [isolated-execution-design.md](./isolated-execution-design.md), [execution-controls.md](./execution-controls.md)

This document distinguishes:

- **Implemented (4B)** — present in the current working tree.
- **Verified** — observed in code/tests.
- **Proposed / future** — Phase 4C+ roadmap only.

---

## 1. Executive summary

Rajosa uses a **deterministic orchestrator** that owns typed lifecycle transitions. The existing `AgentAdapter` is used for **planner** and **implementer** roles. Plan approval and runtime execution authorization remain **separate** explicit user gates.

```
POST /tasks
  → planning → validated Plan → awaiting_plan_approval
POST /tasks/{id}/approve-plan {"approved": true}
  → generating → validating
  → completed                         (static-only profiles)
  → awaiting_execution_auth           (python_test_execution)
POST /tasks/{id}/execute-tests {"confirm": true}
  → completed | failed                (via ExecutionService only)
```

Model output never authorizes plan approval, execution, or success.

---

## 2. Implemented Phase 4B behavior

### 2.1 Components

| Area | Location | Behavior |
|---|---|---|
| Orchestrator | `apps/api/app/orchestration/service.py` | Owns create → plan → approval → generate → validate → lifecycle |
| Plan schema | `orchestration/plan.py` | Bounded JSON plan; rejects shell/Docker/exec fields |
| Lifecycle | `orchestration/lifecycle.py` | Explicit states + legal transition table |
| Events | `orchestration/events.py` | In-memory event list on the task (lost on restart) |
| Routes | `api/routes/tasks.py` | `POST /tasks`, `GET /tasks/{id}`, `POST .../approve-plan`, `POST .../execute-tests` |
| Adapter roles | `agents/base.py` | `role: planner \| implementer` (backward-compatible default implementer) |
| Frontend | `task-panel.tsx`, `lib/api.ts` | Plan review + explicit approve/reject; separate execute button |

### 2.2 Lifecycle states (implemented)

| State | Why needed |
|---|---|
| `created` | Task row initialized before planning |
| `planning` | Planner role in progress |
| `awaiting_plan_approval` | Mandatory gate before implementation |
| `generating` | Implementer role producing artifacts |
| `validating` | Deterministic quality gate |
| `awaiting_execution_auth` | Static OK; runtime still requires confirm |
| `completed` | Terminal success per profile rules |
| `failed` | Terminal failure |

Not implemented as separate states: `planned`, `executing`, `cancelled` (execution progress remains in `execution` field).

### 2.3 Legal transitions

```
created → planning | failed
planning → awaiting_plan_approval | failed
awaiting_plan_approval → generating | failed
generating → validating | failed
validating → completed | awaiting_execution_auth | failed
awaiting_execution_auth → completed | failed
completed → ∅
failed → ∅
```

Invalid transitions → HTTP **409**. Missing/invalid bodies → **422**. Unknown task → **404**.

### 2.4 Plan approval contract

`POST /tasks/{task_id}/approve-plan`

```json
{"approved": true}
```

- `approved` is **required** (missing → 422).
- `true` → start implementation at most once (per-task non-blocking lock).
- `false` → reject plan → `lifecycle=failed`; no implementation; no execution.
- Wrong lifecycle / duplicate approval → 409.
- Never inferred from model text, task creation, or defaults.

### 2.5 Completion semantics

| Profile | Completed when |
|---|---|
| Static-only (`generic_text`, `product_catalog_json`, `python_unit_tests`, `python_code`) | Plan approved + implementer succeeds + quality gate passes |
| `python_test_execution` | Above **plus** explicit `execute-tests` with validated `tests_passed=true` |

`status=passed` maps only from `lifecycle=completed`.
`awaiting_execution_auth` uses `status=pending` even if static quality passed.
`execution` remains separate evidence and keep fail-closed semantics.

### 2.6 Plan schema limits (conservative)

| Limit | Value |
|---|---|
| `schema_version` | `"1"` only |
| Max steps | 8 |
| Max goals | 8 |
| Max acceptance checks | 16 |
| Max serialized plan | 12_000 bytes |
| Forbidden keys | `command`, `shell`, `docker`, `env`, `execution`, `authorize_execution`, … |

Server overrides `requires_execution` from the validation profile (model cannot force runtime for static profiles).

### 2.7 Compatibility fields

Task JSON always includes:

- `lifecycle` (new)
- `status` (`pending` \| `passed` \| `failed`) — legacy
- `execution` — unchanged shape
- `plan`, `plan_approved`, `events` — new

### 2.8 Persistence and startup recovery (current)

- Default store is SQLite (`TASK_DB_PATH`, outside task workspaces). Tasks and orchestration events survive API restarts when the same DB path is reused. Tests may still inject `MemoryTaskStore` (no automatic startup recovery for injected stores).
- Single-process topology (4G.1): exactly one API process per SQLite DB/workspace. Supported entry: `make dev-api` → `python -m app.run_dev`. Unsupported: `uvicorn --workers N` (N>1), `WEB_CONCURRENCY>1`, multiple instances sharing one DB. Durability is not exactly-once execution and does not recover every external side effect.
- Startup recovery (owned store only): interruptible lifecycles `created`, `planning`, `generating`, and `validating` are marked `failed` with `error_category=interrupted`. Waiting states (`awaiting_plan_approval`, `awaiting_execution_auth`) and terminals (`completed`, `failed`) are left unchanged. There is no auto-resume of planner, implementer, or runner.
- Workspace reconciliation at startup is read-only (orphan/missing dirs, digest-mirror drift, leftover `_executions`); it does not delete or rewrite artifacts.
- Concurrent approval: in-process per-task lock prevents duplicate implementation **within one process**.
- The per-task lock is held through the implementer call (not a global lock) so a second approval cannot start while the first is still generating.
- On approval, the server freezes `plan.json`, records `plan.approved.sha256`, and passes that exact plan JSON to the implementer inside an `[APPROVED_PLAN]` envelope.

### 2.9 Security invariants (preserved)

1. Task creation never executes code.
2. Plan generation never executes code.
3. Plan approval never executes code by itself.
4. Artifact generation never auto-executes.
5. Profile selection never authorizes execution.
6. Only `execute-tests` + `confirm=true` requests execution.
7. Runner default remains `off`.
8. No host/in-process execution fallback.
9. Seccomp / staging / timeout controls unchanged.
10. Model output cannot change authorization policy.

### 2.10 Explicit Phase 4B non-goals

- SQLite / durable store (deferred to 4E; now delivered in 4E.0–4E.3 — auto-resume / orphan cleanup still deferred)
- Automatic retries / unbounded loops (4F)
- Reviewer / test-analyst agents (4D)
- Artifact versioning system beyond `plan.json` + `result.txt`
- Enabling `EXECUTION_RUNNER` by default
- Live SumoPod in automated tests

---

## 3. Example API shapes

### Create (returns awaiting approval)

```bash
curl -s http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title":"Hello","instruction":"Write a greeting","validation_profile":"generic_text"}'
```

Response includes `lifecycle: "awaiting_plan_approval"`, `status: "pending"`, `plan: {...}`.

### Approve plan

```bash
curl -s http://127.0.0.1:8000/tasks/<id>/approve-plan \
  -H 'Content-Type: application/json' \
  -d '{"approved":true}'
```

### Reject plan

```bash
curl -s http://127.0.0.1:8000/tasks/<id>/approve-plan \
  -H 'Content-Type: application/json' \
  -d '{"approved":false}'
```

### Execute tests (runtime profiles only, after static success)

```bash
curl -s http://127.0.0.1:8000/tasks/<id>/execute-tests \
  -H 'Content-Type: application/json' \
  -d '{"confirm":true}'
```

---

## 4. Roadmap remaining

| Phase | Status |
|---|---|
| 4A Design | Done |
| 4B Minimal orchestrator + mandatory plan approval | **Done** |
| 4C Structured plan v2 + strict validation | **Done** — see [phase-4c-design.md](./phase-4c-design.md) |
| 4D Artifact provenance / optional reviewer | Not started |
| 4E SQLite persistence | **4E.0–4E.3 done** (protocol + SQLite + integrity + startup recovery/reconcile); auto-resume / orphan cleanup deferred |
| 4F Bounded retry + evaluation harness | Not started |

---

## 5. Historical Phase 4A notes

The original 4A design recommended a deterministic coordinator over multi-agent frameworks, mandatory separate execution authorization, and deferred SQLite. User-approved 4B decisions:

1. Mandatory explicit plan approval before implementation.
2. Keep in-memory store (SQLite → 4E).
3. `completed` follows selected validation profile requirements.
4. Add `lifecycle` while preserving `status` and `execution`.
