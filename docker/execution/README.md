# Local execution image (`rajosa-exec-python:3.12-pytest`)

Lab-only image for isolated pytest runs. **Not production-secure.**

## Built image (Phase 3C)

| Field | Value |
|---|---|
| Tag | `rajosa-exec-python:3.12-pytest` |
| Image ID | `sha256:1d573e2e0dd9c4ec8810993ea0e6c6c691758db0d882ece21df3843565e87d06` |
| Arch | `arm64` / `linux` |
| Base | `python:3.12-slim` (`sha256:c22acf14854c…`, digest `python@sha256:2c941e860699f878…`) — local cache, no pull |
| Python | 3.12.14 |
| pytest | 9.1.1 |
| Other | pluggy 1.6.0, packaging 26.3, pygments 2.21.0, `py.py` shim |
| Build | `docker build --network=none --pull=false` |

## Offline rebuild

```bash
./docker/execution/prepare-vendor-offline.sh
docker build --network=none --pull=false \
  -t rajosa-exec-python:3.12-pytest \
  -f docker/execution/Dockerfile \
  docker/execution
```

- Vendor source: `apps/api/.venv/lib/python3.12/site-packages` only
- Build context: `vendor/` only (see `.dockerignore`)
- No PyPI, no `docker pull`, no app repo / `.env` in the image

## Seccomp profile (per container)

Pinned official profile (not `main`):

- Path: `docker/execution/seccomp/default.json`
- Provenance: `docker/execution/seccomp/PROVENANCE.txt`
- Tag: `moby/profiles` `seccomp/v0.2.1` @ `5ad5f40…`
- SHA-256: `536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74`

The API runner validates checksum/structure and passes `--security-opt seccomp=<absolute-path>`. It never falls back to `unconfined`.

## Keep runner off by default

`EXECUTION_RUNNER=off` remains the default. Opt-in steps are documented in `docs/execution-controls.md`.
