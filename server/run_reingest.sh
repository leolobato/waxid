#!/usr/bin/env bash
# Spin a local server for a full library re-ingest (no auto-reload).
#
# Usage: ./run_reingest.sh [db-path]        default: ./data/reingest.db
#
# Re-ingest is idempotent: an existing track (same album + track name) gets
# its hashes replaced in place and keeps curated metadata. To preserve the
# production library's Discogs links and side/position edits, copy the
# production fingerprints.db (and its sibling covers/ dir) to the db-path
# before starting; otherwise a fresh library is built from scratch.
#
# After ingesting, reclaim the space freed by replaced hashes:
#   sqlite3 <db-path> "VACUUM;"
# then move the .db file plus the covers/ dir next to it onto the server.
set -euo pipefail

cd "$(dirname "$0")"

if [ -d "venv" ]; then
    source venv/bin/activate
fi

export WAXID_DB_PATH="${1:-./data/reingest.db}"
mkdir -p "$(dirname "$WAXID_DB_PATH")"

echo "WaxID re-ingest server on http://localhost:8457 (DB: $WAXID_DB_PATH)"
exec uvicorn app.main:app --host 0.0.0.0 --port 8457
