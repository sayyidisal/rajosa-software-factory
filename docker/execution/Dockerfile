# Rajosa local execution image — Python 3.12 + pytest (offline-friendly).
#
# Base MUST already exist locally (no pull during build):
#   python:3.12-slim  (inspected Phase 3C: sha256:c22acf14854c... arm64)
#
# Vendor MUST be prepared from the local apps/api/.venv site-packages
# via prepare-vendor-offline.sh BEFORE build (no PyPI access).
#
# Build (after vendor prep) — do not use --pull:
#   docker build --network=none -t rajosa-exec-python:3.12-pytest -f docker/execution/Dockerfile docker/execution
#
# This image is for local lab use only. Not a production sandbox.

FROM python:3.12-slim

# Drop to a documented non-root identity matching the runner (--user 65534:65534).
# Keep image minimal: no apt install, no pip network, no app secrets, no repo copy.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/rajosa-pytest

COPY vendor/ /opt/rajosa-pytest/

# Smoke-check at build time that pytest imports (offline).
RUN python -c "import pytest; print('pytest', pytest.__version__)"

USER 65534:65534
WORKDIR /workspace

# Default command is unused; the application supplies a fixed argv allowlist.
CMD ["python", "-m", "pytest", "--version"]
