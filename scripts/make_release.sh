#!/usr/bin/env bash
# ============================================================================
# fnack release bundle builder — deploy to other machines WITHOUT git.
#
# Creates fnack-release-<version>.tar.gz containing the full buildable source
# (only git-tracked files, so cookies / config DBs / .venv are excluded by
# construction). Copy the tarball to another machine and run:
#
#     tar xzf fnack-release-<version>.tar.gz
#     cd fnack
#     cp .env.example .env          # then set MUSIC_PATH to your library
#     docker compose up -d --build
#
# With internet access you can skip the build entirely (GitHub Actions
# publishes ghcr.io/tajanthind/fnack:latest on every push):
#     docker compose up -d          # pulls the prebuilt image from GHCR
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

VER="$(sed -n 's/__version__ = "\(.*\)"/\1/p' version.py)"
if [ -z "$VER" ]; then
    echo "ERROR: could not read version from version.py" >&2
    exit 1
fi

OUT="fnack-release-${VER}.tar.gz"

# git archive exports only tracked files — never cookies, secrets, DBs or
# local state. Anything needed for a clean build must be committed first.
git archive --format=tar.gz --prefix=fnack/ -o "$OUT" HEAD

echo
echo "Created: $(pwd)/$OUT ($(du -h "$OUT" | cut -f1))"
echo
echo "Deploy on another machine (no git needed):"
echo "  1. Copy $OUT to the target machine (scp / USB / any transfer)."
echo "  2. tar xzf $OUT"
echo "  3. cd fnack && cp .env.example .env"
echo "     (edit .env -> MUSIC_PATH=<path to your music library>)"
echo "  4. docker compose up -d --build"
echo
echo "Optional: publish to GHCR first (docker compose pull on targets)"
