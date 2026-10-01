#!/bin/bash
# Build and (re)start the Toledo containers, stamping them with the current
# commit and build time. docker-compose.yml reads TOLEDO_COMMIT and
# TOLEDO_BUILD_TIME from the environment as build args; this is what sets them
# — without it, both fall back to "unknown" inside the image (it has no .git).
set -euo pipefail
cd "$(dirname "$0")/.."

export TOLEDO_COMMIT="$(git rev-parse --short HEAD)"
export TOLEDO_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "Building $TOLEDO_COMMIT, built $TOLEDO_BUILD_TIME"
docker compose up -d --build "$@"
