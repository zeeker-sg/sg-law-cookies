#!/usr/bin/env bash
# Full VPS cycle: wait for upstream builds, refresh Datasette, pull the
# canonical cookies DB from S3, ingest new rows from every active source,
# push the updated DB back, then build and deploy the site.
# Designed to run on a schedule (cron / systemd timer).
#
# Order matters:
#   1. Wait for upstream zeeker builds to finish (so Datasette has fresh data)
#   2. Trigger a Datasette refresh (pull latest DBs from S3 into the container)
#   3. Backup local DB to S3 (preserve any cookies approved via Discord
#      since the last cycle), then restore the canonical DB from S3
#   4. Ingest all active sources from Datasette
#   5. Backup + build + deploy
#
# Requires in .env (same directory as the repo root):
#   S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_ENDPOINT_URL
#   COOKIES_LLM_BACKEND=ollama
#   OLLAMA_MODEL=gemma4:31b-cloud                  # VPS cloud-routed model
#   COOKIES_DUAL_OLLAMA=true                       # enable dual backends
#   JUDGMENT_OLLAMA_HOST=http://127.0.0.1:11434
#   JUDGMENT_OLLAMA_MODEL=kimi-k2.6:cloud            # cloud-routed model for judgments
#
# Both news and judgments use the VPS Ollama with cloud-routed models.
# JUDGMENT_OLLAMA_HOST/MODEL in .env controls the judgment backend.
#
# Cloudflare Pages deploy needs `npx wrangler login` done once on the host.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a

LIMIT="${COOKIES_RUN_LIMIT:-100}"
JUDGMENT_HOST="${JUDGMENT_OLLAMA_HOST:-http://127.0.0.1:11434}"
JUDGMENT_ACTIVE=true

# ── Wait for upstream zeeker builds to finish ──────────────────────
# The upstream builds (sglawwatch, sg-gov-newsrooms, zeeker-judgements, etc.)
# run via cron and write progress JSON files to /tmp/zeeker-progress-*.json.
# Cookies reads from Datasette (127.0.0.1:8001), which pulls from S3 via the
# datasette-refresh script. If we ingest while a build is still deploying,
# we'll miss the new rows. So we wait for any in-progress builds to finish.
BUILD_WAIT_TIMEOUT="${COOKIES_BUILD_WAIT_TIMEOUT:-3600}"  # default 60 min
BUILD_CHECK_INTERVAL=30  # seconds between checks

wait_for_builds() {
    local elapsed=0
    local builds_running
    while (( elapsed < BUILD_WAIT_TIMEOUT )); do
        # Detect running upstream builds by checking /proc for processes
        # whose argv[0] is the zeeker-build binary. This avoids false
        # positives from this script's own command line containing the
        # string "zeeker build" in comments/patterns.
        builds_running=0
        for pid in $(pgrep -f 'zeeker-build' 2>/dev/null || true); do
            [[ $pid -eq $$ ]] && continue
            [[ $pid -eq $PPID ]] && continue
            local cmd
            cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
            if [[ "$cmd" =~ ^"$HOME"/.local/bin/zeeker-build\  ]]; then
                builds_running=$((builds_running + 1))
            fi
        done
        if (( builds_running == 0 )); then
            if (( elapsed > 0 )); then
                echo "    all upstream builds finished (waited ${elapsed}s)"
            fi
            return 0
        fi
        if (( elapsed == 0 )); then
            echo "    upstream builds still running — waiting (timeout ${BUILD_WAIT_TIMEOUT}s)..."
        fi
        sleep "$BUILD_CHECK_INTERVAL"
        elapsed=$(( elapsed + BUILD_CHECK_INTERVAL ))
        if (( elapsed % 300 == 0 )); then
            echo "    still waiting for ${builds_running} build(s) (${elapsed}s elapsed)..."
        fi
    done
    echo "    WARNING: build wait timeout (${BUILD_WAIT_TIMEOUT}s) exceeded — proceeding anyway"
    return 1
}

echo "==> waiting for upstream zeeker builds"
wait_for_builds || true

# ── Refresh Datasette from S3 ──────────────────────────────────────
# Trigger a Datasette refresh so the latest built DBs are served at
# 127.0.0.1:8001 before we ingest. The system crontab only refreshes every
# 3 hours, which can lag behind the build schedule by hours.
DATASETTE_REFRESH_SCRIPT="$HOME/bin/cron/zeeker-datasette-refresh.sh"
if [[ -x "$DATASETTE_REFRESH_SCRIPT" ]]; then
    echo "==> refreshing Datasette from S3"
    "$DATASETTE_REFRESH_SCRIPT" || echo "    WARNING: Datasette refresh failed — proceeding with existing data"
else
    echo "==> Datasette refresh script not found at ${DATASETTE_REFRESH_SCRIPT} — skipping"
fi

# ── Judgment Ollama health check ────────────────────────────────────
echo "==> checking judgment Ollama (${JUDGMENT_HOST})"
if curl -sf "${JUDGMENT_HOST}/api/tags" >/dev/null 2>&1; then
    echo "    judgment Ollama is UP — judgments will run"
else
    echo "    judgment Ollama is DOWN — skipping judgment sources"
    JUDGMENT_ACTIVE=false
fi

DB_PATH="${COOKIES_DB_PATH:-./cookies.db}"

echo "==> backup local DB to S3 (preserve any approved cookies before restore)"
uv run cookies backup

# ── Preserve discord_msg_id mappings across restore ────────────────
# The S3 canonical DB may be stale w.r.t. which pending cookies have
# already been posted to Discord.  Without this, post_pending_cookies
# re-posts every pending cookie after every vps_cycle, creating
# duplicate Discord cards and losing track of what's been reviewed.
# We dump {cookie_id: discord_msg_id} for all pending rows that have
# a msg_id, then restore them after the S3 pull.
DISCORD_MSG_MAP=""
if sqlite3 "$DB_PATH" "SELECT count(*) FROM pending_cookies WHERE discord_msg_id IS NOT NULL" 2>/dev/null | grep -q '[1-9]'; then
    DISCORD_MSG_MAP=$(mktemp)
    sqlite3 -json "$DB_PATH" \
        "SELECT id, discord_msg_id FROM pending_cookies WHERE discord_msg_id IS NOT NULL" \
        > "$DISCORD_MSG_MAP" 2>/dev/null
    PRESERVED=$(python3 -c "import json,sys; print(len(json.load(sys.stdin)))" < "$DISCORD_MSG_MAP" 2>/dev/null || echo 0)
    echo "    preserving $PRESERVED discord_msg_id mapping(s) across restore"
else
    echo "    no discord_msg_id mappings to preserve"
fi

echo "==> restore canonical DB from S3"
uv run cookies restore

# Re-apply preserved discord_msg_id mappings after restore.
if [[ -n "$DISCORD_MSG_MAP" && -f "$DISCORD_MSG_MAP" ]]; then
    uv run python - "$DISCORD_MSG_MAP" <<'PYMSG'
import json, sqlite3, sys, os
map_file = sys.argv[1]
db_path = os.environ.get("COOKIES_DB_PATH", "./cookies.db")
with open(map_file) as f:
    rows = json.load(f)
conn = sqlite3.connect(db_path)
applied = 0
for row in rows:
    cur = conn.execute(
        "UPDATE pending_cookies SET discord_msg_id = ? WHERE id = ? AND discord_msg_id IS NULL",
        (row["discord_msg_id"], row["id"]),
    )
    applied += cur.rowcount
conn.commit()
conn.close()
print(f"    re-applied {applied} discord_msg_id mapping(s) after restore")
PYMSG
    rm -f "$DISCORD_MSG_MAP"
fi

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
      echo "--> ${src} [SKIPPED — judgment Ollama offline]"
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