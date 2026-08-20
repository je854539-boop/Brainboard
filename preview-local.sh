#!/usr/bin/env bash
# One-shot local live preview -- run this ON YOUR OWN COMPUTER (not in any
# Claude session). Brings up Postgres + the API in Docker, runs migrations,
# seeds demo data if the DB is empty, and opens the dashboard in your
# browser at http://localhost:8000. Safe to re-run any time you `git pull`
# new work -- it rebuilds the image and re-runs migrations, and leaves your
# data alone (Postgres persists in a docker-compose volume).
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "==> No .env found, creating one from .env.example"
  echo "    (blank API keys are fine just to preview the UI -- those"
  echo "    integrations no-op instead of erroring without a key)"
  cp .env.example .env
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

# Local preview only: give the api container a published port so your
# browser can reach it directly on localhost. Production (deploy.sh) keeps
# this unset on purpose and goes through Caddy instead -- see the comment
# in docker-compose.yml.
if ! grep -qF '      - "8000:8000"' docker-compose.yml; then
  echo "==> Publishing api container's port 8000 to localhost for this preview"
  python3 - <<'PY'
path = "docker-compose.yml"
text = open(path).read()
marker = "    volumes:\n      - ./backend:/app\n"
inserted = '    ports:\n      - "8000:8000"\n'
if marker in text and inserted not in text:
    text = text.replace(marker, marker + inserted, 1)
    open(path, "w").write(text)
PY
fi

echo "==> Building and starting Postgres + API"
docker compose up -d --build postgres api

echo "==> Waiting for API health check"
for i in $(seq 1 60); do
  if curl -sf http://localhost:8000/healthz > /dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "==> Running Alembic migrations"
docker compose exec -T api alembic upgrade head

LEAD_COUNT=$(docker compose exec -T postgres psql -U "${POSTGRES_USER:-brainboard}" -d "${POSTGRES_DB:-brainboard}" -tAc "SELECT count(*) FROM master_log_entries" 2>/dev/null | tr -d '[:space:]' || echo "")
if [ "$LEAD_COUNT" = "0" ]; then
  echo "==> Database is empty, seeding demo data"
  docker compose exec -T api python seed.py
else
  echo "==> Database already has data ($LEAD_COUNT leads), skipping seed"
fi

echo ""
echo "==> Live preview ready: http://localhost:8000"
echo "    Pipeline:  http://localhost:8000/master-log"
echo "    Globe:     http://localhost:8000/globe"
echo ""
echo "Re-run this script any time after 'git pull' to pick up new changes."
