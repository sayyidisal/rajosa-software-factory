#!/usr/bin/env bash
# Copy pytest + required deps from the local API venv into ./vendor.
# No network. No PyPI. Safe to run offline.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$ROOT/../.." && pwd)"
SITE="${REPO_ROOT}/apps/api/.venv/lib/python3.12/site-packages"
VENDOR="${ROOT}/vendor"

if [[ ! -d "$SITE" ]]; then
  echo "missing local venv site-packages: $SITE" >&2
  exit 1
fi

# Replace package trees only; keep vendor/.gitignore if present.
mkdir -p "$VENDOR"
shopt -s nullglob dotglob
for entry in "$VENDOR"/* "$VENDOR"/.[!.]* "$VENDOR"/..?*; do
  base="$(basename "$entry")"
  [[ "$base" == "." || "$base" == ".." ]] && continue
  [[ "$base" == ".gitignore" ]] && continue
  rm -rf "$entry"
done
shopt -u nullglob dotglob

# Minimal set observed in apps/api/.venv for pytest 9.x on Python 3.12.
# Transitive runtime imports required by pytest 9.x in this venv (all local copies).
# Includes pytest's py.py shim (single-file) and pygments (terminalwriter).
PACKAGES=(pytest _pytest pluggy iniconfig packaging pygments py)

for name in "${PACKAGES[@]}"; do
  if [[ -d "$SITE/$name" ]]; then
    cp -R "$SITE/$name" "$VENDOR/$name"
  elif [[ -f "$SITE/$name.py" ]]; then
    cp "$SITE/$name.py" "$VENDOR/$name.py"
  else
    echo "required package missing in venv: $name" >&2
    exit 1
  fi
  # Copy matching dist-info for auditability when present.
  shopt -s nullglob
  for meta in "$SITE/${name}"-*.dist-info; do
    cp -R "$meta" "$VENDOR/$(basename "$meta")"
  done
  shopt -u nullglob
done

# Record provenance for auditors (no secrets).
{
  echo "prepared_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "source_site=$SITE"
  echo "packages=${PACKAGES[*]}"
  "$REPO_ROOT/apps/api/.venv/bin/python" - <<'PY'
import pytest, pluggy, iniconfig, packaging, pygments
print(f"pytest={pytest.__version__}")
print(f"pluggy={pluggy.__version__}")
print(f"iniconfig={getattr(iniconfig, '__version__', 'unknown')}")
print(f"packaging={packaging.__version__}")
print(f"pygments={pygments.__version__}")
print("py_shim=py.py")
PY
} > "$VENDOR/PROVENANCE.txt"

echo "vendor prepared at $VENDOR"
cat "$VENDOR/PROVENANCE.txt"
