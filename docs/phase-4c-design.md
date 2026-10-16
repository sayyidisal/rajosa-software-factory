# Phase 4C — Structured Planning Contracts & Deterministic Plan Validation

**Status:** Implemented
**Date:** 2026-10-15
**Baseline commit (4B):** `d3235a6007021a0506475811c06a7d237ea8fdde`
**Depends on:** Phase 4B (approval-gated orchestration)
**Related:** [agent-orchestration-design.md](./agent-orchestration-design.md), [architecture.md](./architecture.md)

This document distinguishes:

- **Implemented (4C)** — present in the working tree after this phase.
- **Verified** — observed against Phase 4B baseline before 4C.
- **Deferred** — explicitly out of Phase 4C.

---

## 1. Executive summary

Phase 4C tightens plan contracts to **`schema_version: "2"`** with **strict JSON parsing**, **`extra='forbid'`**, structured acceptance checks with stable IDs, profile coherence rules, and an **approval digest** that binds the canonical plan **and** the server-trusted `validation_profile`.

Plan validation improves **contract reliability**. It does **not** prove generated code is correct, approve plans, authorize execution, or replace `BasicQualityGate` / `ExecutionService`.

### Approved decisions (locked for implementation)

1. Accept schema version `"2"` only (`"1"` rejected).
2. Strict JSON: reject duplicate keys, NaN/Infinity, unknown fields, prose-wrapped JSON, and Markdown fences. **No silent repair.**
3. Approval integrity binds canonical validated plan + trusted `validation_profile` via documented digest preimage (§6).
4. Require ≥1 acceptance check, unique IDs, and valid step→check references; every check must be referenced by ≥1 step.
5. Empty `expected_artifacts` per step is allowed; task-level artifact / quality-profile contracts remain authoritative after generation.
6. **No broad text denylist** as a security boundary. Structural forbidden-field checks only. Legitimate technical wording (e.g. “docker”, “shell”, “environment”) in descriptions is allowed.
7. Acceptance-check declarations stay separate from verification (quality gate / execution).

---

## 2. Verified Phase 4B gaps (pre-4C)

| Input | 4B behavior | 4C fix |
|---|---|---|
| Unknown fields | Accepted (Pydantic ignore) | `extra='forbid'` |
| Duplicate JSON keys | Accepted | Strict `object_pairs_hook` |
| NaN/Infinity | Accepted | `parse_constant` reject |
| Markdown fences | Silently unwrapped | Rejected |
| Prose-wrapped JSON | Rejected | Unchanged (fail closed) |
| Acceptance checks | Free-text strings | Structured `{check_id, description, kind}` |
| Approval hash | Plan JSON only | Plan JSON + validation_profile |

---

## 3. Goals and non-goals

**Goals:** v2 contract, strict parse, semantic ID/ref/profile rules, shared entrypoint for mock/SumoPod, approval digest with profile, offline tests, docs.

**Non-goals:** step DAGs, new agent roles, orchestration frameworks, SQLite, retries, enabling the execution runner, treating plan checks as proof of runtime success.

---

## 4. Implemented contract (`schema_version: "2"`)

### Root `Plan`

| Field | Rules |
|---|---|
| `schema_version` | Literal `"2"` |
| `summary` | 1–500 chars (stripped) |
| `goals` | 1–8 strings, each 1–200 chars |
| `steps` | 1–8 `PlanStep` |
| `acceptance_checks` | 1–16 `AcceptanceCheck` |
| `requires_execution` | bool; **server overrides** from validation profile |

### `PlanStep`

| Field | Rules |
|---|---|
| `step_id` | `^[a-zA-Z0-9_-]{1,32}$`, unique |
| `description` | 1–500 chars |
| `expected_artifacts` | 0–8 names; path escape forbidden; empty allowed |
| `acceptance_check_ids` | 1–8 refs to known `check_id`s |

### `AcceptanceCheck`

| Field | Rules |
|---|---|
| `check_id` | `^[a-zA-Z0-9_-]{1,32}$`, unique; must be referenced by ≥1 step |
| `description` | 1–200 chars |
| `kind` | `human_review` \| `artifact_present` \| `quality_profile` \| `runtime_tests` |

### Profile coherence

- Runtime profile (`python_test_execution`): must include a `runtime_tests` check; server sets `requires_execution=true`.
- Static profiles: must not include `runtime_tests`; server sets `requires_execution=false`.

### Bounds

Max input/canonical 12_000 bytes; nesting depth 8; 32 keys/object; 16 array length; forbidden structural keys include `command`, `shell`, `docker`, `env`, `execution`, `authorize_execution`, `confirm`, `tools`, `permissions`, `depends_on`, …

---

## 5. Parsing and validation algorithm

1. Reject empty / oversized raw text.
2. Reject if Markdown fence `` ``` `` present anywhere.
3. `parse_strict_json` (duplicate keys + non-finite rejected).
4. Root must be object; `schema_version == "2"`.
5. Walk tree for depth/size/forbidden keys.
6. Pydantic validate with `extra='forbid'`.
7. Semantic: unique IDs, refs, orphans, profile rules.
8. Apply `requires_execution` override from trusted profile.
9. Re-check canonical size.

**No fence unwrap. No prose extraction.**

---

## 6. Approval digest preimage (exact)

Function: `compute_approval_digest(plan, validation_profile)` in `orchestration/plan.py`.

**UTF-8 preimage bytes:**

```text
rajosa-plan-approval-v1\n
<validation_profile>\n
<canonical_plan_json>
```

Where:

- `validation_profile` is the **server-trusted** task profile (no newlines).
- `canonical_plan_json` is `Plan.model_dump_json()` (compact Pydantic dump).
- Digest = `SHA-256(preimage).hexdigest()`, stored as `plan.approved.sha256` and checked before implementer invocation.
- Implementer receives the **exact** canonical JSON inside `[APPROVED_PLAN …]`.

**Guarantees:** Detects divergence of approved in-memory plan or profile binding within one process.
**Does not guarantee:** plan “safety”, code quality, or that acceptance-check kinds were satisfied.

---

## 7. Error categories

`empty_plan`, `plan_too_large`, `markdown_fence_forbidden`, `malformed_json`, `duplicate_key`, `nonfinite_number`, `invalid_root`, `unsupported_schema_version`, `forbidden_field`, `schema_invalid`, `duplicate_id`, `invalid_reference`, `profile_incompatible`, `invalid_profile`.

Public errors: `Plan validation failed: <safe detail>` — no secrets; raw may be truncated to workspace `plan.raw.txt`.

---

## 8. Security and lifecycle invariants (preserved)

- Plan generation / approval never execute code.
- Model cannot self-approve or authorize execution.
- Execution only via `ExecutionService` + required `confirm: true`.
- Runner default remains `off`.
- Lifecycle table unchanged; invalid plan → `failed` from `planning`.
- Quality gate remains independent of plan self-assessment.

---

## 9. Test matrix (implemented)

Covered in `tests/test_orchestration.py` and related suites: valid min/full v2; malformed/prose/fence/dup-key/NaN; unknown/missing/unsupported version/size/dup IDs; invalid refs; profile incompat; technical prose allowed; digest changes with plan or profile; implementer envelope; no implementer on invalid plan; create/approve never call runner; confirm required; lifecycle regressions; SumoPod planner prompt mentions v2 (unit).

---

## 10. Files (implementation)

| Area | Files |
|---|---|
| Strict JSON | `orchestration/strict_json.py` |
| Plan v2 | `orchestration/plan.py` |
| Digest + parse wiring | `orchestration/service.py` |
| Adapters | `agents/{base,mock,sumopod}.py` |
| Tests / fixtures | `tests/orchestration_helpers.py`, `test_orchestration.py`, related FixedAdapters |
| Frontend | `lib/api.ts`, `task-panel.tsx` |
| Docs | this file, `architecture.md`, `agent-orchestration-design.md` |

---

## 11. Deferred

Step DAGs; reviewer agents; SQLite (4E); retries (4F); auto-verifying acceptance-check kinds inside the quality gate; broad NLP denylists.

---

## 12. Definition of done

- [x] Design decisions locked and documented
- [x] Plan v2 + strict JSON + semantic rules
- [x] Approval digest binds plan + profile
- [x] Mock / SumoPod planner updated
- [x] Offline tests green
- [x] Frontend types/display updated
- [x] Runner default off; no live APIs in tests
