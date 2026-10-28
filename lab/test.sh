#!/usr/bin/env bash
# Run pytest inside the CWA runtime with this checkout mounted at /src.
#   lab/test.sh tests/unit -m unit
# Optional:
#   POSITION_BRIDGE_REAL_LIBRARY=<dir>  real-library <id>.epub/<id>.kepub pairs
#   READEST_CLONE=<dir>                 a Readest clone, for its CREngine oracle
set -euo pipefail
cd "$(dirname "$0")/.."
image=cwa-position-bridge-test
if ! docker image inspect "$image" >/dev/null 2>&1; then
  docker build --platform linux/amd64 -q -t "$image" -f lab/Dockerfile.test lab >/dev/null
fi
mounts=()
if [ -n "${POSITION_BRIDGE_REAL_LIBRARY:-}" ]; then
  mounts+=(-v "$POSITION_BRIDGE_REAL_LIBRARY":/real:ro -e POSITION_BRIDGE_REAL_LIBRARY=/real)
fi
if [ -n "${READEST_CLONE:-}" ]; then
  mounts+=(-v "$READEST_CLONE":/readest:ro -e READEST_CLONE=/readest)
fi
exec docker run --rm --platform linux/amd64 \
  -v "$PWD":/src -w /src -e PYTHONPATH=/src \
  ${mounts[@]+"${mounts[@]}"} \
  --entrypoint /lsiopy/bin/python3 "$image" -m pytest "$@"
