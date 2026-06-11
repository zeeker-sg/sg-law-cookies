#!/usr/bin/env bash
# Build the site from the database and deploy to Cloudflare Pages.
# First-time setup: npx wrangler login, then
#   npx wrangler pages project create sg-law-cookies --production-branch main
# and attach the custom domain cookies.zeeker.sg in the Cloudflare dashboard
# (Pages project -> Custom domains) or via:
#   npx wrangler pages domain add cookies.zeeker.sg --project-name sg-law-cookies
set -euo pipefail
cd "$(dirname "$0")/.."

uv run cookies build --out dist
npx wrangler pages deploy dist --project-name sg-law-cookies --commit-dirty=true
