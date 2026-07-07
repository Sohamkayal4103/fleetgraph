#!/usr/bin/env bash
# Start the FleetGraph backend (:8099) and web UI (:5180) together.
set -euo pipefail
cd "$(dirname "$0")"
set -a; [ -f .env ] && . ./.env; set +a
if [ -z "${NEO4J_PASSWORD:-}" ]; then echo "!! Set NEO4J_PASSWORD in .env (copy .env.example)"; fi
echo "backend  -> http://localhost:8099"
echo "frontend -> http://localhost:5180"
.venv/bin/uvicorn fleetgraph.api:app --port 8099 --reload --reload-dir fleetgraph --log-level warning &
API=$!
( cd web && npm run dev ) &
WEB=$!
trap "kill $API $WEB 2>/dev/null || true" EXIT INT TERM
wait
