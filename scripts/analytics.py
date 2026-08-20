#!/usr/bin/env python3
"""Fetch Cloudflare Analytics for a specific hostname (e.g., data.zeeker.sg or cookies.zeeker.sg).

Usage:
    uv run python scripts/analytics.py [--host data.zeeker.sg] [--days 7] [--format table|json]

Requires CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID in .env or environment.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ENV_PATH = Path.home() / ".config" / "zeeker" / ".env"


def _load_env(path: Path) -> dict:
    """Read raw key=value pairs from a .env file (no shell expansion)."""
    vals = {}
    if path.exists():
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k] = v
    return vals


def _require(var: str, env: dict) -> str:
    val = os.getenv(var) or env.get(var)
    if not val:
        print(f"ERROR: {var} is required. Add it to ~/.config/zeeker/.env", file=sys.stderr)
        sys.exit(1)
    return val


def format_table(rows: list[dict], title: str) -> str:
    if not rows:
        return f"\n{title}\n  (no data)\n"
    keys = list(rows[0].keys())
    widths = {k: max(len(k), *(len(str(r.get(k, ""))) for r in rows)) for k in keys}
    sep = " | ".join("=" * widths[k] for k in keys)
    header = " | ".join(k.ljust(widths[k]) for k in keys)
    lines = [f"\n{title}", sep, header, sep]
    for r in rows:
        lines.append(" | ".join(str(r.get(k, "")).ljust(widths[k]) for k in keys))
    lines.append(sep)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Cloudflare Analytics for zeeker.sg subdomains")
    parser.add_argument("--host", default="cookies.zeeker.sg", help="Target hostname (default: cookies.zeeker.sg)")
    parser.add_argument("--days", type=int, default=7, help="Number of days to look back (default: 7)")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Output format")
    parser.add_argument("--save", type=Path, default=None, help="Save JSON to file")
    args = parser.parse_args()

    env = _load_env(ENV_PATH)
    token = _require("CLOUDFLARE_API_TOKEN", env)
    zone_id = _require("CLOUDFLARE_ZONE_ID", env)

    # Cloudflare analytics window (UTC) — GraphQL expects YYYY-MM-DD
    until = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    since = until - timedelta(days=args.days)
    since_str = since.strftime("%Y-%m-%d")
    until_str = until.strftime("%Y-%m-%d")

    # httpRequestsAdaptiveGroups supports clientRequestHTTPHost filtering
    # but has a 1-day max range, so we loop day-by-day.
    # httpRequests1dGroups gives richer stats (pageViews, threats, uniques) but
    # does NOT support host filtering — we use it for zone-level totals only.
    per_host_q = """
    query($zoneTag: String!, $since: Time!, $until: Time!, $host: String!) {
      viewer {
        zones(filter: { zoneTag: $zoneTag }) {
          httpRequestsAdaptiveGroups(
            limit: 100,
            filter: { date_geq: $since, date_lt: $until, clientRequestHTTPHost: $host }
          ) {
            count
            dimensions { date clientRequestHTTPHost }
          }
        }
      }
    }
    """

    daily_breakdown = []
    total_requests = 0
    day = since
    while day < until:
        day_str = day.strftime("%Y-%m-%d")
        next_day = day + timedelta(days=1)
        next_str = next_day.strftime("%Y-%m-%d")
        gql_resp = httpx.post(
            "https://api.cloudflare.com/client/v4/graphql",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": per_host_q, "variables": {"zoneTag": zone_id, "since": day_str, "until": next_str, "host": args.host}},
            timeout=30,
        )
        gql = gql_resp.json()
        if gql_resp.status_code != 200 or gql.get("errors"):
            # Skip days with errors rather than aborting
            daily_breakdown.append({"date": day_str, "requests": 0})
            day = next_day
            continue
        zones = (gql.get("data", {}).get("viewer", {}).get("zones", []) or [{}])[0]
        groups = zones.get("httpRequestsAdaptiveGroups", [])
        day_count = sum(int(g.get("count", 0)) for g in groups)
        total_requests += day_count
        daily_breakdown.append({"date": day_str, "requests": day_count})
        day = next_day

    # Zone-level totals for richer metrics (pageViews, threats, uniques, cachedRequests)
    zone_q = """
    query($zoneTag: String!, $since: Time!, $until: Time!) {
      viewer {
        zones(filter: { zoneTag: $zoneTag }) {
          httpRequests1dGroups(
            limit: 100,
            filter: { date_geq: $since, date_leq: $until },
            orderBy: [date_ASC]
          ) {
            dimensions { date }
            sum { requests pageViews threats cachedRequests }
            uniq { uniques }
          }
        }
      }
    }
    """
    zone_resp = httpx.post(
        "https://api.cloudflare.com/client/v4/graphql",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": zone_q, "variables": {"zoneTag": zone_id, "since": since_str, "until": until_str}},
        timeout=30,
    )
    zone_data = zone_resp.json()
    zone_groups = []
    if not zone_data.get("errors"):
        zone_groups = (zone_data.get("data", {}).get("viewer", {}).get("zones", []) or [{}])[0].get("httpRequests1dGroups", [])

    zone_totals = {
        "pageviews": sum(int(g["sum"]["pageViews"]) for g in zone_groups),
        "uniques":   sum(int(g["uniq"]["uniques"]) for g in zone_groups),
        "threats":   sum(int(g["sum"]["threats"]) for g in zone_groups),
        "requests":  sum(int(g["sum"]["requests"]) for g in zone_groups),
        "cached":    sum(int(g["sum"].get("cachedRequests", 0)) for g in zone_groups),
    }

    # Per-host requests from adaptive groups; zone-level for the rest
    totals = {
        "requests":  total_requests,  # per-host, accurate
        "pageviews": zone_totals["pageviews"],  # zone-level, approximate
        "uniques":   zone_totals["uniques"],    # zone-level, approximate
        "threats":   zone_totals["threats"],    # zone-level, approximate
        "cached":    zone_totals["cached"],     # zone-level, approximate
    }

    report = {
        "site": args.host,
        "zone_id": zone_id,
        "period_days": args.days,
        "since": since_str,
        "until": until_str,
        "totals": totals,
        "daily_breakdown": daily_breakdown,
        "note": "requests are per-host; pageviews/uniques/threats/cached are zone-level (all zeeker.sg subdomains)",
    }

    if args.format == "json":
        out = json.dumps(report, indent=2, default=str)
        print(out)
    else:
        print(format_table(report["daily_breakdown"], f"Daily Traffic — {args.host} ({args.days} days)"))
        print(f"\nTotals\n  Page views: {totals['pageviews']:,}")
        print(f"  Unique visitors: {totals['uniques']:,}")
        print(f"  Threats blocked: {totals['threats']:,}")
        print(f"  Requests: {totals['requests']:,}")

    if args.save:
        with open(args.save, "w") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"\nSaved to {args.save}")


if __name__ == "__main__":
    main()
