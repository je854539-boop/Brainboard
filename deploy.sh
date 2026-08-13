#!/usr/bin/env bash
# Manual, one-command deploy -- run this ON THE VPS (or via `ssh <vps> 'cd
# /path/to/Brainboard && ./deploy.sh'` from anywhere with SSH access to it).
#
# Deliberately NOT wired to auto-run on every push -- a human (you, or me
# when you ask) decides when a build goes live on the box actually placing
# real calls to real merchants. Pulls the latest commit on the current
# branch, rebuilds the API image, runs pending Alembic migrations, and
# restarts -- Postgres data survives via the docker-compose volume.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Pulling latest code ($(git branch --show-current))"
git pull --ff-only

echo "==> Rebuilding and restarting containers"
docker compose up -d --build

echo "==> Waiting for API health check"
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/healthz > /dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "==> Running Alembic migrations"
docker compose exec -T api alembic upgrade head

echo "==> Deployed. Current revision:"
docker compose exec -T api alembic current

echo "==> Recent container status"
docker compose ps
