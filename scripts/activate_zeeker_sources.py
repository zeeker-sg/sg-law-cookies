"""One-off: activate the remaining upstream Zeeker sources and seed a launch
watermark so their first run starts fresh (2026-06-08) instead of backfilling
the oldest rows. Excludes sg-law-cookies/* (this project's own outputs)."""

from sg_law_cookies import db
from sg_law_cookies.config import load_settings

SEED = "2026-06-08T00:00:00"
TARGETS = [
    ("pdpc", "enforcement_decisions"),
    ("sg-gov-newsrooms", "acra_news"),
    ("sg-gov-newsrooms", "agc_news"),
    ("sg-gov-newsrooms", "ccs_news"),
    ("sg-gov-newsrooms", "ipos_news"),
    ("sg-gov-newsrooms", "judiciary_news"),
    ("sg-gov-newsrooms", "mlaw_news"),
    ("sg-gov-newsrooms", "mom_news"),
    ("sg-gov-newsrooms", "pdpc_news"),
    ("sglawwatch", "about_singapore_law"),
    ("sglawwatch", "commentaries"),
    ("sglawwatch", "headlines"),
]


def main() -> None:
    settings = load_settings()
    conn = db.init_db(settings.db_path)
    entries = {(e.zeeker_db, e.table): e for e in db.list_registry(conn)}
    for zdb, table in TARGETS:
        entry = entries.get((zdb, table))
        if entry is None:
            print(f"SKIP {zdb}/{table}: not in registry")
            continue
        db.upsert_registry_entry(conn, entry.model_copy(update={"active": True}))
        existing = db.get_watermark(conn, zdb, table)
        if existing is None:
            db.set_watermark(conn, zdb, table, SEED)
            print(f"ACTIVATED {zdb}/{table} (pipeline={entry.pipeline}) watermark<-{SEED}")
        else:
            print(f"ACTIVATED {zdb}/{table} (pipeline={entry.pipeline}) watermark kept={existing}")


if __name__ == "__main__":
    main()
