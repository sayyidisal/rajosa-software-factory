# Phase 3B/3C — Local isolated Python test execution

Local-only, explicitly authorized Python test runs. **Not production-secure.** Container isolation is a containment aid, not a guarantee against escape.

## Runtime inspection

### Phase 3B (earlier)

Docker CLI present; daemon not running; image missing.

### Phase 3C — daemon up + offline image build (2026-10-15)

| Check | Result |
|---|---|
| Docker daemon | Available (Engine 28.1.1, Desktop 4.41.2, `linux/arm64`) |
| Base `python:3.12-slim` | Local `sha256:c22acf14854c…` / digest `python@sha256:2c941e860699f878…` — **CACHED, no pull** |
| Build | `docker build --network=none --pull=false` |
| Image `rajosa-exec-python:3.12-pytest` | **Built** `sha256:1d573e2e0dd9…` (`arm64`, ~157MB) |
| Python / pytest in image | 3.12.14 / 9.1.1 |
| Other vendored deps | pluggy 1.6.0, packaging 26.3, pygments 2.21.0, `py.py` shim |
| Network / registry during build | **None** (`--network=none`, base layer CACHED) |
| Default config | `EXECUTION_RUNNER=off` (`.env` untouched) |

## Explicit authorization (separate gates)

Authentication, plan approval, and runner execution are **distinct**. A valid
`Authorization: Bearer` token authenticates task routes; it does **not** by
itself authorize plan approval outcomes or container execution.

1. Authenticate: every `/tasks*` call requires a configured bearer matching
   `RAJOSA_API_TOKEN` (fail-closed if unset/short).
2. Create task (`POST /tasks`) with the intended `validation_profile`.
   Create generates a structured plan and stops at `awaiting_plan_approval`.
   Create never starts the execution runner (`execution.status=not_requested`).
3. Approve the plan explicitly: `POST /tasks/{id}/approve-plan` with
   `{"approved": true}` (or `false` to reject).
   - Under the current orchestrator, `approved: true` synchronously runs
     implementation and the static quality gate in that request.
   - Static gate success may leave the task at `lifecycle=completed` with
     legacy `status=passed` for profiles that do **not** require runtime
     (e.g. `generic_text`). That means static checks passed — **not** that
     generated code was executed or is runtime-correct.
   - For `validation_profile=python_test_execution`, approval leaves the task
     at `lifecycle=awaiting_execution_auth` with `execution.status=not_requested`.
4. Execution runner (separate gate, default **off**): only after approval, and
   only when the runner is explicitly enabled, call
   `POST /tasks/{id}/execute-tests` with `{"confirm": true}`.

Profile alone / create-task / model text / bearer presence never authorize
isolated execution. `EXECUTION_RUNNER=off` remains the default.

## Disable (default)

```bash
EXECUTION_RUNNER=off
```

## Manual opt-in (not applied in Phase 3C)

Do **not** change `.env` unless you explicitly choose to. After accepting residual risk:

```bash
EXECUTION_RUNNER=local_container
EXECUTION_IMAGE=rajosa-exec-python:3.12-pytest
```

Restart API; confirm `GET /health` shows `"execution_runner":"local_container"`.

Phase 3C application E2E used an **injected** `LocalDockerExecutionRunner` in-process (TestClient) so defaults/` .env` stayed off.

## Image build (offline)

See `docker/execution/`:

```bash
./docker/execution/prepare-vendor-offline.sh
docker build --network=none --pull=false \
  -t rajosa-exec-python:3.12-pytest \
  -f docker/execution/Dockerfile \
  docker/execution
```

Vendor copies **only** from `apps/api/.venv` site-packages: `pytest`, `_pytest`, `pluggy`, `iniconfig`, `packaging`, `pygments`, `py.py`. Context limited by `.dockerignore` to `vendor/`.

## Limits

| Limit | Default | Hard max |
|---|---|---|
| Wall clock | 30s | 60s |
| Memory | 512 MiB | 1024 MiB |
| Concurrency | 1 | 1 |

## Control verification matrix (Phase 3C post-build)

| Control | Status | Evidence |
|---|---|---|
| Feature disabled by default | **VERIFIED** | health `execution_runner=off`; `.env` has no `EXECUTION_*` |
| Explicit authorization | **VERIFIED** | `confirm=false` → 422; create leaves `not_requested` |
| No execution on task create | **VERIFIED** | unit + E2E |
| No host/in-process fallback | **VERIFIED** | Docker runner only; disabled path separate |
| Fixed argv / no shell | **VERIFIED** | code + unit |
| No docker.sock | **VERIFIED** | live probe `docker_sock False` |
| Host secrets not in job | **VERIFIED** | live: probe secrets absent |
| `--network none` | **VERIFIED** | live: no eth0; external connect fails |
| Non-root | **VERIFIED** | live uid 65534; image `User=65534:65534` |
| Read-only root + tmp/staging write | **VERIFIED** | live probe |
| Cap drop + no-new-privileges | **VERIFIED** | CapEff/CapBnd 0; NoNewPrivs 1 |
| Wall-clock timeout | **VERIFIED** | app E2E timed_out in ~2.2s; `tests_passed=false` |
| Memory/CPU/PIDs enforcement | **VERIFIED** (Phase 3D) | cgroup observable + bounded OOM/fork/CPU iter tests; see Phase 3D section |
| Seccomp syscall filter (daemon default) | **NOT VERIFIED** / absent globally | Daemon still `seccomp=unconfined` (unchanged by design) |
| Seccomp per execution container | **VERIFIED** (Phase 3D.1) | Pinned `moby/profiles` `seccomp/v0.2.1`; `/proc` `Seccomp: 2` / `Seccomp_filters: 1`; fail-closed if missing/altered |
| Staging/symlink/traversal | **VERIFIED** | unit tests |
| Cleanup | **VERIFIED** | unit + E2E staging dirs empty after run |
| Fail-closed / `tests_passed` rules | **VERIFIED** | unit + E2E fail/timeout |
| Pinned image present + auditable | **VERIFIED** | built offline; Dockerfile + vendor provenance |
| Benign pytest pass (image) | **VERIFIED** | 3 passed |
| Benign pytest fail (image) | **VERIFIED** | exit 1 |
| App E2E pass via execute-tests | **VERIFIED** | injected runner; `tests_passed=true` |
| App E2E fail via execute-tests | **VERIFIED** | `tests_failed`; `tests_passed=false` |
| Production-complete sandbox | **UNSUPPORTED** | Docker Desktop VM; not claimed |

## Phase 3D — Seccomp & resource enforcement audit (2026-10-15)

### Seccomp

| Question | Evidence |
|---|---|
| Source of `Seccomp: 0` | `/proc/self/status` **inside** the container (not Docker Desktop UI) |
| Meaning | Linux: `0` = seccomp disabled; `1` = strict; `2` = filter. Also `Seccomp_filters: 0` |
| Runner argv | Passes `--security-opt no-new-privileges:true` only — **no** `--security-opt seccomp=...` |
| Daemon | `docker info`: `SecurityOptions=["name=seccomp,profile=unconfined","name=cgroupns"]` and warning **“daemon is not using the default seccomp profile”** |
| Conclusion (pre-3D.1) | Daemon default remains unconfined. |
| Phase 3D.1 | Runner applies pinned profile per container: `docker/execution/seccomp/default.json` (`moby/profiles` tag `seccomp/v0.2.1`, SHA-256 `536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74`). Live: `Seccomp: 2`, `Seccomp_filters: 1`. Missing/invalid/altered profile → `seccomp_unavailable`, never `unconfined`. **Active filter ≠ full syscall-policy audit.** |

### Resources (configured → observable → behavioral)

| Limit | Configured | Observable | Behavioral |
|---|---|---|---|
| Memory 512 MiB (default) | `--memory` / `--memory-swap` | cgroup `memory.max=536870912` | 64 MiB limit + 80 MiB alloc → exit **137** (OOM) |
| CPU 1.0 (default) | `--cpus` | cgroup `cpu.max` matches quota | 0.1 vs 1.0 busy loop: ~0.96M vs ~18M iters / 2s |
| PIDs 64 (default) | `--pids-limit` | cgroup `pids.max` | limit 8: fork fails with `BlockingIOError` errno 11 after 7 children |
| Wall clock | app `subprocess` timeout | — | E2E ~2.2s timeout (Phase 3C) |
| Stdout/stderr | `truncate_bytes` / limits | — | unit: 20 KiB → truncated at 16 KiB |

### Staging / mounts

- Single bind: staging → `/workspace` (RW). No docker.sock. Host user home **not** visible (`/Users/...` absent). Empty `/home` is image FS, not a host mount.
- Root overlay `ro`; `/tmp` tmpfs; writes outside workspace/tmp fail.
- Staging traversal/symlink rejects + cleanup: unit tests.

### Image provenance (unchanged from 3C)

- Image `sha256:1d573e2e0dd9…` arm64; Python 3.12.14; pytest 9.1.1
- Base `python@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a`
- `GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305` = official CPython release-key id baked into `python:3.12-slim`, **not** a host/API secret

## Residual risks

- Docker Desktop on macOS ≠ hardened bare-metal Linux.
- Staging bind-mount is writable inside the container.
- Daemon seccomp remains globally unconfined; only execution containers get the pinned profile via the runner.
- `Seccomp: 2` proves filter mode is on, not that every dangerous syscall is blocked.
- Image-baked `GPG_KEY` is a public packaging key id, not a host secret.
- Enabling `EXECUTION_RUNNER=local_container` in `.env` remains a manual opt-in.

**Do not claim production readiness or complete sandbox security.**
