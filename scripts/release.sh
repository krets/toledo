#!/bin/bash
# Build and (re)start the Toledo containers. A plain `docker compose up -d
# --build` already stamps the image with the commit and build time (the
# Dockerfile reads them from .git); this script only makes the stamp explicit
# by passing them as TOLEDO_COMMIT and TOLEDO_BUILD_TIME build args.
set -euo pipefail
cd "$(dirname "$0")/.."

export TOLEDO_COMMIT="$(git rev-parse --short HEAD)"
export TOLEDO_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "Building $TOLEDO_COMMIT, built $TOLEDO_BUILD_TIME"
docker compose up -d --build "$@"
