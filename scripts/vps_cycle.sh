#!/usr/bin/env bash
# Full VPS cycle: pull the canonical DB from S3, ingest new rows from every
# active source, push the updated DB back, then build and deploy the site.
# Designed to run on a schedule (cron / systemd timer).
#
# Order matters: `restore` FIRST so the host always continues from canonical
# state instead of re-forking from a stale local copy (see backup.py).
#
# Requires in .env (same directory as the repo root):
#   S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_ENDPOINT_URL
#   COOKIES_LLM_BACKEND=ollama
#   OLLAMA_MODEL=gemma4:26b        # a LOCAL model — NOT a :cloud or -mlx variant
# and a local Ollama (OLLAMA_HOST, default http://localhost:11434) serving that
# model. Structured-output extraction only works on local models; :cloud models
# silently ignore the JSON schema and the pipeline produces garbage.
#
# Cloudflare Pages deploy needs `npx wrangler login` done once on the host.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a

LIMIT="${COOKIES_RUN_LIMIT:-100}"

echo "==> restore canonical DB from S3"
uv run cookies restore

echo "==> ingest active sources (limit ${LIMIT}/source)"
uv run python - <<'PY' | while read -r src; do
from sg_law_cookies import db
from sg_law_cookies.config import load_settings

conn = db.init_db(load_settings().db_path)
for entry in db.list_registry(conn):
    if entry.active:
        print(f"{entry.zeeker_db}/{entry.table}")
PY
  echo "--> ${src}"
  uv run cookies run "${src}" --limit "${LIMIT}" </dev/null
done

echo "==> backup updated DB to S3"
uv run cookies backup

echo "==> build + deploy site"
uv run cookies build --out dist
npx wrangler pages deploy dist --project-name sg-law-cookies --commit-dirty=true

echo "==> cycle complete"
