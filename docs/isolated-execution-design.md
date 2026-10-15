# Phase 3A — Isolated Code Execution: Architecture and Threat Model

Status: **design only**. No runner is implemented. Generated code must not be executed by the application until a later, explicitly approved implementation phase.

Related current state (as of Phase 2.2.1):

- FastAPI `TaskService` runs an agent adapter, writes to `LocalWorkspace`, then evaluates `BasicQualityGate`.
- Validation profiles include `python_unit_tests` and `python_code` with AST static checks only.
- Quality results may include `test_structure_valid` / `code_structure_valid`.
- The platform must **not** emit `tests_passed` unless a trusted isolated runner actually executes tests.
- Workspace path containment exists; it is **not** an execution sandbox.

---

## 1. Threat model

Untrusted AI-generated Python (and its tests) is hostile by default.

| ID | Threat | Example | Mitigations | Residual risk |
|---|---|---|---|---|
| T1 | Secret / credential theft | Read `SUMOPOD_API_KEY`, `.env`, SSH keys, cloud tokens | Do not pass host env into runner; empty env allowlist only; no bind-mount of secrets; separate credentials store for online later | Kernel or hypervisor bugs; operator misconfiguration |
| T2 | Host filesystem read/write | Escape workspace; overwrite app code | Per-task volume only; read-only rootfs; no Docker socket; path + symlink validation before mount | Container breakout |
| T3 | Network / exfiltration | DNS/HTTP to attacker; SSRF to metadata | Network none by default; explicit future allowlist only | Shared kernel side channels; future allowlist mistakes |
| T4 | Resource exhaustion | Fork bomb; memory bomb; disk fill | CPU/memory/pids/disk quotas; concurrent job caps | Noisy neighbor on shared host if quotas weak |
| T5 | Hang / infinite loop | Busy loop; deadlock | Hard wall-clock timeout; kill process tree; cancel path | Kill races leave orphans if runtime buggy |
| T6 | Malicious imports / installs | `pip install`; `ctypes` to load `.so`; `os.system` | No network; no package install in job; fixed image with frozen deps; disallow shell | Vulnerable packages in the fixed image |
| T7 | Path traversal / symlink | `../`, link to `/etc` | Validate artifact paths; refuse symlinks leaving workspace; copy (not bind) staged files when possible | TOCTOU between validate and run |
| T8 | Oversized I/O | Huge stdout; gigabyte artifact | Cap stdout/stderr bytes; cap artifact bytes/count; fail closed | Partial truncation may hide useful failure detail |
| T9 | Container escape | Kernel exploit; privileged flags | Never privileged; drop caps; no host PID/net/IPC; non-root; seccomp/AppArmor where available | Escape remains possible against imperfect isolation |
| T10 | Concurrency contention | Many jobs starve host / each other | Global and per-user quotas; queue; fair scheduling later | Local PoC may be single-user only |

Assumption: the attacker controls generated source and tests inside the task workspace, and may try to influence any string that reaches the runner. The attacker must not control container image, mounts, command template, or privileges.

---

## 2. Isolation options

### Option A — Restricted local container runner (Docker/Podman/containerd)

**Idea:** API stages a per-task directory, starts a disposable container from a pinned image, runs an allowlisted command (e.g. `python -m pytest`), collects exit code and capped logs, destroys the container.

| Dimension | Assessment |
|---|---|
| Security boundary | Process + mount + (optional) userns/seccomp. Shared host kernel. |
| Operational complexity | Medium on developer machines; image build/pull required. |
| Cost | Low for local use. |
| Apple Silicon | Feasible with Docker Desktop / Colima / Podman machine (ARM64 images). |
| Future server | Same pattern scales to a worker host; still shared kernel. |
| Networking | Can set `--network=none`. |
| Filesystem | Bind or copy one task dir; rootfs read-only. |
| Resources | CPU/memory/pids via runtime flags; disk via volume size or quotas. |

**Pros:** Familiar; good enough for local gated execution; maps cleanly to a later remote worker.  
**Cons:** Not a perfect sandbox; Docker socket must never be exposed to jobs; Desktop VM quirks on macOS.

### Option B — Dedicated remote execution service

**Idea:** API sends a signed execution request (artifacts + profile) to a separate service that runs jobs on isolated workers (containers or VMs). Results return over a narrow API.

| Dimension | Assessment |
|---|---|
| Security boundary | Stronger separation from API secrets and host. |
| Operational complexity | High (service, auth between services, scaling). |
| Cost | Higher. |
| Apple Silicon | Local API can call remote Linux/ARM workers. |
| Future server | Natural fit for online deployment. |
| Networking | Job network still default-deny on workers. |

**Pros:** API host never executes untrusted code; clearer blast radius.  
**Cons:** Overkill for current local PoC; needs auth, TLS, quotas, observability.

### Option C — Stronger sandbox runtime (gVisor / Firecracker / WASM)

| Runtime | Fit |
|---|---|
| gVisor | Better syscall filtering on Linux workers; not the default macOS local path. |
| Firecracker microVM | Strong isolation for multi-tenant online; heavy for laptop PoC. |
| WASM (Wasmtime, etc.) | Attractive for limited Python subsets; poor compatibility with arbitrary pytest + CPython ecosystem today. |

**Pros:** Stronger than plain containers when available.  
**Cons:** Tooling and Python compatibility trade-offs; not first choice for “run normal pytest fixtures” on Apple Silicon laptops.

### Recommendation (staged)

1. **Local now (Phase 3B candidate):** Option A — restricted local container runner behind a `CodeExecutionRunner` interface, optional and off by default.  
2. **Online later:** Move the same interface to Option B workers; consider gVisor/Firecracker on Linux hosts for multi-tenant exposure.  
3. **Do not** run generated code in-process in FastAPI (`subprocess` of host Python, `eval`, `exec`).

Containerization is a control layer, not a guarantee of complete isolation.

---

## 3. Proposed architecture

### Current flow (today)

```text
Client -> Next.js /backend -> FastAPI TaskService
  -> AgentAdapter -> LocalWorkspace artifacts
  -> BasicQualityGate (static / deterministic)
  -> Task status passed|failed
```

### Target flow (future, not implemented)

```text
Client -> FastAPI TaskService
  -> AgentAdapter -> workspace artifacts
  -> BasicQualityGate (static first; fail closed)
  -> if profile requires execution and runner enabled:
       StagingCopy -> CodeExecutionRunner.run(ExecutionRequest)
         -> isolated runtime (allowlisted command only)
         -> ExecutionResult
  -> QualityGate merges static + execution checks
  -> Task status + truthful execution fields
```

### Responsibilities

| Component | Responsibility |
|---|---|
| `TaskService` | Orchestrate adapter → static gate → optional execution → final status. Never invent `tests_passed`. |
| `CodeExecutionRunner` (interface) | Accept `ExecutionRequest`, return `ExecutionResult`. Hide runtime details. |
| `LocalContainerRunner` (future impl) | Start/stop container; enforce limits; no host env passthrough. |
| `RemoteExecutionClient` (later) | Same interface to remote workers. |
| Workspace staging | Copy allowlisted files into an ephemeral execution directory; reject escapes/symlinks out. |
| Quality gate | Map `ExecutionResult` to checks; keep static checks independent. |

### Proposed interfaces (illustrative)

```python
class CodeExecutionRunner(Protocol):
    def run(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute an allowlisted job. Must not use caller env or host secrets."""


class ExecutionRequest(BaseModel):
    task_id: str
    runtime: Literal["python3.12-pytest"]  # allowlist only
    profile: Literal["python_unit_tests", "python_code"]
    workspace_snapshot_path: str  # host path prepared by staging, not by the model
    timeout_seconds: int  # capped by server config
    # No arbitrary argv, shell, env map, or mount list from the task payload.


class ExecutionResult(BaseModel):
    execution_started: bool
    execution_completed: bool
    timed_out: bool
    runner_unavailable: bool
    exit_code: int | None
    tests_executed: bool
    tests_passed: bool  # true only if trusted runner verified success
    stdout_truncated: str
    stderr_truncated: str
    error_code: Literal[
        "none",
        "timeout",
        "cancelled",
        "limit_exceeded",
        "invalid_request",
        "runner_error",
        "infra_error",
    ]
    checks: list[CheckResult]
```

### Allowlisted runtimes and commands

Server-defined only, for example:

| Runtime ID | Image (pinned digest) | Command template |
|---|---|---|
| `python3.12-pytest` | `rajosa-exec-python:3.12@sha256:…` | `python -m pytest -q --maxfail=20` |
| `python3.12-compileall` | same image | `python -m compileall -q .` |

Task input may select only a runtime ID already in the allowlist. It must not supply shell strings, extra args, env vars, or mount paths.

### Workspace staging

1. After static validation passes (or in parallel only for non-executing checks), copy allowlisted artifacts into `exec-staging/<task_id>/`.
2. Reject absolute paths, `..`, and symlinks that resolve outside the task workspace.
3. Enforce total file count and byte budget.
4. Mount/copy that directory as the only writable workspace in the job (`/workspace`).
5. Delete staging and container artifacts on all termination paths.

### Timeouts, cancellation, cleanup, failures

| Event | Behavior |
|---|---|
| Wall-clock timeout | Kill job; `timed_out=true`; `tests_passed=false` |
| Client/API cancel (future) | Same kill + cleanup |
| Limit exceeded | Stop job; `limit_exceeded` |
| Runner missing (Docker down) | `runner_unavailable=true`; task failed; not a test failure |
| Non-zero pytest exit | `tests_executed=true`, `tests_passed=false` |
| Success | `tests_executed=true`, `tests_passed=true` only after verified exit code and result contract |

Cleanup must run after success, failure, timeout, cancel, and runner crash (best-effort + sweep).

### Quality gate integration

Suggested additional checks (future):

| Check name | Meaning |
|---|---|
| `execution_started` | Runner accepted and started the job |
| `execution_completed` | Job finished without infra abort |
| `tests_executed` | Allowlisted test command ran |
| `tests_passed` | Trusted verification of success |
| `execution_timed_out` | Failed check when timeout |
| `runner_available` | Failed when runner cannot start |

Static checks remain mandatory. Execution checks are additive and fail-closed.

---

## 4. Security requirements

### Mandatory for local gated execution (before enabling any runner)

- Non-root user inside the job.
- Read-only root filesystem; writable only `/workspace` (and maybe `/tmp` with size cap).
- Per-task isolated workspace; no access to `apps/api/.env`, repo, or home.
- Network disabled by default.
- No Docker socket, cloud metadata, or host credentials mounted.
- CPU, memory, pids, wall-clock timeout, stdout/stderr byte caps, artifact size caps.
- Pinned image by digest; no in-job package install.
- Path/symlink validation before staging.
- Cleanup on every termination path.
- Concurrency cap (e.g. 1–2 local jobs).
- Structured logs: `task_id`, `runtime`, `error_type`, durations — never prompts, full source, API keys, Authorization headers.
- Versioned runner config (allowlist + limits) in repo or signed file.

### Mandatory before exposing the service to the internet

Everything above, plus:

- Authentication and authorization on the API.
- Per-tenant quotas and abuse controls.
- TLS between clients and API; authenticated channel to remote workers if used.
- Stronger isolation (dedicated workers; prefer gVisor/microVM for multi-tenant).
- Image provenance, vulnerability scanning, and update process.
- Audit trail of executions (metadata only).
- No public access to runner management APIs.
- Legal/ops incident response for breakout scenarios.
- Default deny for any network egress from jobs unless separately approved.

---

## 5. Truthful execution status

Recommended task/result fields (future API extension, backward compatible defaults):

| Field / check | When true |
|---|---|
| Static validation passed | Existing gate checks all pass |
| `execution_started` | Runner began the job |
| `execution_completed` | Job ended without infra loss |
| `tests_executed` | Allowlisted test command ran |
| `tests_passed` | Runner verified success (exit + agreed result contract) |
| Timed out | Wall clock exceeded |
| Execution failed | Non-zero exit or invalid outputs |
| Runner unavailable | Cannot start runtime / pull image / daemon down |

Rules:

- `tests_passed` implies `tests_executed` and `execution_completed`.
- Infrastructure errors (`runner_unavailable`, `infra_error`) are not represented as “tests failed” alone; include distinct error codes.
- Absence of a runner must not invent success; profiles that require execution should fail with `runner_unavailable` when disabled.

State sketch:

```text
static_failed -> task failed (no execution)
static_ok + execution_disabled_but_required -> failed (runner_unavailable or config)
static_ok + execution_enabled -> execution_started
  -> timed_out | limit_exceeded | runner_error | completed
completed + tests_executed + exit 0 + verified -> tests_passed
completed + tests_executed + exit != 0 -> tests failed (task failed)
```

---

## 6. Future test plan (design requirements only — not executed in Phase 3A)

| Area | Planned tests |
|---|---|
| Benign fixture | Known-good pytest project passes end-to-end through runner |
| Timeout / cancel | Infinite loop killed; cancel cleans up |
| Memory / output limits | Allocation and huge print fail closed |
| Network isolation | Egress attempt fails (no route / blocked) |
| Filesystem isolation | Cannot read host `.env` or write outside `/workspace` |
| Path / symlink | Traversal and escaping symlinks rejected at staging |
| Cleanup | No leftover containers/dirs after success/fail/timeout/cancel |
| Invalid runtime | Unknown runtime ID rejected before start |
| Concurrency / quotas | Excess jobs rejected or queued per policy |
| Forged results | TaskService ignores untrusted client-supplied execution claims |
| Runner crash / unavailable | Distinct failed status; no `tests_passed` |

Unit tests should mock the runner interface; integration tests against containers belong in a dedicated optional suite, not the default `make test-api` path that must stay offline and non-executing for generated code.

---

## 7. Local development approach (future)

1. Keep default: no execution (`EXECUTION_RUNNER=off`).
2. Developers opt in: install container engine, pull pinned image, set `EXECUTION_RUNNER=local_container`.
3. `make test-api` remains free of untrusted code execution and paid provider calls.
4. Optional `make test-runner-integration` (future) behind explicit flag for container tests.
5. Document Apple Silicon prerequisites (ARM64 image, Docker/Podman availability).

---

## 8. Open questions requiring approval

1. Is Docker Desktop / Colima / Podman acceptable as the local runtime prerequisite?
2. Should `python_unit_tests` **require** execution when a runner is configured, or remain static-only unless a new profile (e.g. `python_unit_tests_executed`) is selected?
3. Maximum wall-clock, memory, and concurrency for local laptops?
4. May jobs ever receive any network (e.g. to a mock HTTP fixture), or hard none forever until online phase?
5. Copy vs bind-mount for staging (copy safer, slower)?
6. Timeline for remote workers vs staying local-container only?
7. Who owns image rebuilds and CVE response?

---

## 9. Residual risks

- Shared-kernel container escape.
- Misconfiguration mounting secrets or enabling network.
- Supply-chain risk in the pinned base image.
- Mac Docker VM still shares the developer machine’s broader trust boundary.
- Resource limits that are too high still allow DoS of the laptop.
- Incomplete cleanup under rare crash conditions without a sweeper.

---

## 10. Non-goals for Phase 3A

- Implementing any runner.
- Adding subprocess/eval/exec/Docker files to the app.
- Changing current API behavior or quality gate semantics beyond this document.
- Claiming production readiness for untrusted code execution.
