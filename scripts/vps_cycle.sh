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
#   OLLAMA_MODEL=gemma4:31b-cloud                  # VPS cloud-routed model
#   COOKIES_DUAL_OLLAMA=true                       # enable dual backends
#   JUDGMENT_OLLAMA_HOST=http://houfus-macbook-pro:11434
#   JUDGMENT_OLLAMA_MODEL=gemma4:26b-mlx-64k      # Mac Ollama for judgments
#
# News sources use the VPS Ollama (cloud models OK here); judgments use the
# Mac Ollama (local models required for reliable structured-output extraction).
#
# Cloudflare Pages deploy needs `npx wrangler login` done once on the host.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a

LIMIT="${COOKIES_RUN_LIMIT:-100}"
JUDGMENT_HOST="${JUDGMENT_OLLAMA_HOST:-http://houfus-macbook-pro:11434}"
JUDGMENT_ACTIVE=true

# ── Mac Ollama health check ──────────────────────────────────────────
echo "==> checking Mac Ollama (${JUDGMENT_HOST})"
if curl -sf "${JUDGMENT_HOST}/api/tags" >/dev/null 2>&1; then
    echo "    Mac Ollama is UP — judgments will run"
else
    echo "    Mac Ollama is DOWN — skipping judgment sources"
    JUDGMENT_ACTIVE=false
fi

echo "==> restore canonical DB from S3"
uv run cookies restore

echo "==> ingest active sources (limit ${LIMIT}/source)"
uv run python - <<'PY' | while IFS='|' read -r src pipeline; do
from sg_law_cookies import db
from sg_law_cookies.config import load_settings

conn = db.init_db(load_settings().db_path)
for entry in db.list_registry(conn):
    if entry.active:
        print(f"{entry.zeeker_db}/{entry.table}|{entry.pipeline}")
PY
  if [[ "$pipeline" == "judgment" ]] && [[ "$JUDGMENT_ACTIVE" != "true" ]]; then
      echo "--> ${src} [SKIPPED — Mac Ollama offline]"
      continue
  fi
  echo "--> ${src}"
  uv run cookies run --source "${src}" --limit "${LIMIT}" </dev/null || true
done

echo "==> backup updated DB to S3"
uv run cookies backup

echo "==> build + deploy site"
uv run cookies build --out dist
npx wrangler pages deploy dist --project-name sg-law-cookies --commit-dirty=true

echo "==> cycle complete"
