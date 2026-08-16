#!/usr/bin/env python3
"""Auto-approve pending cookies older than 72 hours.

Promotes any pending cookie that has been sitting in the review queue
for more than max_age_hours (default 72) without any reviewer action.
This prevents backlog while giving a generous review window.

Runs as a Hermes cron job (every 1 hour).

Requires in .env:
    COOKIES_DB_PATH — path to cookies.db (defaults to ./cookies.db)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    load_dotenv()
    cookies_env = Path(__file__).resolve().parent.parent / ".env"
    if cookies_env.is_file():
        load_dotenv(cookies_env)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

    from sg_law_cookies import db
    from sg_law_cookies.config import load_settings

    settings = load_settings()
    conn = db.init_db(settings.db_path)

    max_age = int(os.environ.get("COOKIES_AUTO_APPROVE_HOURS", "72"))

    promoted = db.auto_approve_pending(conn, max_age_hours=max_age)

    if promoted:
        print(f"auto-approved {len(promoted)} cookie(s) (older than {max_age}h)")
        for cid in promoted:
            print(f"  {cid[:8]}")
    else:
        print(f"no cookies to auto-approve (threshold: {max_age}h)")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())