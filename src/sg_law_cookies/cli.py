"""Command-line entry point: init-db, discover, run, stats (PRD section 8)."""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import anthropic
import httpx

from sg_law_cookies import db
from sg_law_cookies.config import Settings, load_settings
from sg_law_cookies.llm import AnthropicBackend, OllamaBackend
from sg_law_cookies.models import SourceRegistryEntry
from sg_law_cookies.pipeline import PipelineError, run_source
from sg_law_cookies.sitegen import build_site
from sg_law_cookies.zeeker import ZeekerClient, item_type_for


def _cmd_init_db(args: argparse.Namespace, settings: Settings) -> int:
    db.init_db(settings.db_path)
    print(f"initialised {settings.db_path}")
    return 0


def _cmd_discover(args: argparse.Namespace, settings: Settings) -> int:
    """Diff the Zeeker catalogue against the source registry (PRD section 3.2).

    New tables are added inactive with a suggested pipeline; activation is a
    deliberate registry edit. Licence labels are refreshed on every run.
    """
    conn = db.init_db(settings.db_path)
    client = ZeekerClient()
    known = {(e.zeeker_db, e.table): e for e in db.list_registry(conn)}
    new_count = 0
    for entry in client.discover_catalogue():
        key = (entry.database, entry.table)
        if key in known:
            db.upsert_registry_entry(
                conn, known[key].model_copy(update={"license": entry.license})
            )
        else:
            new_entry = SourceRegistryEntry(
                zeeker_db=entry.database,
                table=entry.table,
                pipeline=item_type_for(entry.table),
                license=entry.license,
                active=False,
            )
            db.upsert_registry_entry(conn, new_entry)
            new_count += 1
            print(
                f"NEW: {entry.database}/{entry.table} "
                f"(license: {entry.license}, suggested pipeline: {new_entry.pipeline}) "
                "— inactive until reviewed"
            )
    print(f"registry: {len(db.list_registry(conn))} tables, {new_count} new")
    return 0


def _set_active(args: argparse.Namespace, settings: Settings, active: bool) -> int:
    """Flip the registry active flag for one <database>/<table> source."""
    zeeker_db, _, table = args.source.partition("/")
    if not zeeker_db or not table:
        print(
            f"error: source must be '<database>/<table>', got {args.source!r}",
            file=sys.stderr,
        )
        return 1
    conn = db.init_db(settings.db_path)
    entry = next(
        (
            e
            for e in db.list_registry(conn)
            if e.zeeker_db == zeeker_db and e.table == table
        ),
        None,
    )
    if entry is None:
        print(
            f"error: no registry entry for {args.source}; "
            "run 'cookies discover' first",
            file=sys.stderr,
        )
        return 1
    db.upsert_registry_entry(conn, entry.model_copy(update={"active": active}))
    state = "active" if active else "inactive"
    print(
        f"{args.source}: {state} "
        f"(pipeline: {entry.pipeline}, license: {entry.license})"
    )
    return 0


def _cmd_activate(args: argparse.Namespace, settings: Settings) -> int:
    return _set_active(args, settings, active=True)


def _cmd_deactivate(args: argparse.Namespace, settings: Settings) -> int:
    return _set_active(args, settings, active=False)


def _build_llm(settings: Settings) -> AnthropicBackend | OllamaBackend | None:
    if settings.llm_backend == "ollama":
        return OllamaBackend(model=settings.ollama_model, host=settings.ollama_host)
    if settings.llm_backend != "anthropic":
        print(f"error: unknown COOKIES_LLM_BACKEND {settings.llm_backend!r}", file=sys.stderr)
        return None
    if not settings.anthropic_api_key:
        print(
            "error: ANTHROPIC_API_KEY is required unless --dry-run "
            "(or set COOKIES_LLM_BACKEND=ollama)",
            file=sys.stderr,
        )
        return None
    return AnthropicBackend(
        anthropic.Anthropic(api_key=settings.anthropic_api_key), model=settings.model
    )


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    conn = db.init_db(settings.db_path)
    zeeker_client = ZeekerClient()
    llm = None
    if not args.dry_run:
        llm = _build_llm(settings)
        if llm is None:
            return 1

    with httpx.Client(timeout=30.0) as folio_client:
        try:
            result = run_source(
                conn,
                zeeker_client,
                llm,
                folio_client,
                source=args.source,
                limit=args.limit,
                dry_run=args.dry_run,
                model=settings.model,
            )
        except PipelineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.dry_run:
        for item in result.dry_run_items:
            print(f"would process: {item.title} ({item.source_url})")
        print(f"{len(result.dry_run_items)} items pending for {args.source}")
    else:
        print(
            f"{args.source}: processed {result.processed} items, "
            f"{len(result.cookies)} cookies, watermark={result.watermark}"
        )
    return 0


def _cmd_stats(args: argparse.Namespace, settings: Settings) -> int:
    conn = db.init_db(settings.db_path)
    day = date.fromisoformat(args.date) if args.date else date.today()
    stats = db.compute_daily_stats(conn, day)
    db.save_daily_stats(conn, stats)
    print(stats.model_dump_json(indent=2))
    return 0


def _cmd_backfill_areas(args: argparse.Namespace, settings: Settings) -> int:
    from sg_law_cookies.backfill import backfill_areas

    conn = db.init_db(settings.db_path)
    llm = _build_llm(settings)
    if llm is None:
        return 1

    def _progress(i, total, cookie, old, new):
        shown = "FAILED" if new is None else (new or ["(none)"])
        print(f"[{i + 1}/{total}] {old or ['General']} -> {shown}  {cookie.headline[:60]}")

    report = backfill_areas(
        conn,
        llm,
        only_general=args.only_general,
        news_only=args.news_only,
        dry_run=args.dry_run,
        progress=_progress,
    )

    verb = "would change" if args.dry_run else "changed"
    print(
        f"\n{report.considered}/{report.total} considered; {verb} {report.changed} "
        f"(+{report.now_tagged} newly tagged, -{report.now_empty} now empty, "
        f"{report.failed} failed)"
    )
    if not args.dry_run and report.changed:
        for day_iso in db.list_cookie_dates(conn):
            db.save_daily_stats(conn, db.compute_daily_stats(conn, date.fromisoformat(day_iso)))
        print(f"recomputed daily stats for {len(db.list_cookie_dates(conn))} days")
    return 0


def _cmd_build(args: argparse.Namespace, settings: Settings) -> int:
    conn = db.init_db(settings.db_path)
    report = build_site(conn, Path(args.out), args.base_url)
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"built {report.pages} pages ({len(report.dates)} days) -> {args.out}")
    return 0


def _cmd_backup(args: argparse.Namespace, settings: Settings) -> int:
    from sg_law_cookies.backup import BackupError, backup_db

    try:
        result = backup_db(settings.db_path)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"backed up {settings.db_path} ({result.size_bytes:,} bytes) -> "
        f"s3://{result.bucket}/{result.dated_key} (+ latest.db)"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cookies", description="SG Law Cookies pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init-db", help="create the SQLite database")
    init_p.set_defaults(func=_cmd_init_db)

    discover_p = sub.add_parser(
        "discover", help="diff the Zeeker catalogue against the source registry"
    )
    discover_p.set_defaults(func=_cmd_discover)

    activate_p = sub.add_parser(
        "activate", help="activate a registry source for processing"
    )
    activate_p.add_argument(
        "source", help="Zeeker source as <database>/<table>, e.g. zeeker-judgements/judgments"
    )
    activate_p.set_defaults(func=_cmd_activate)

    deactivate_p = sub.add_parser(
        "deactivate", help="deactivate a registry source (run will refuse it)"
    )
    deactivate_p.add_argument("source", help="Zeeker source as <database>/<table>")
    deactivate_p.set_defaults(func=_cmd_deactivate)

    run_p = sub.add_parser("run", help="fetch and process new rows for one source")
    run_p.add_argument(
        "--source",
        required=True,
        help="Zeeker source as <database>/<table>, e.g. sglawwatch/headlines",
    )
    run_p.add_argument("--limit", type=int, default=100)
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="list pending items without LLM calls or writes",
    )
    run_p.set_defaults(func=_cmd_run)

    stats_p = sub.add_parser("stats", help="compute and store daily stats")
    stats_p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    stats_p.set_defaults(func=_cmd_stats)

    backfill_p = sub.add_parser(
        "backfill-areas",
        help="re-tag existing cookies' areas of law with the closed-vocabulary classifier",
    )
    backfill_p.add_argument(
        "--only-general",
        action="store_true",
        help="only re-tag cookies that currently have no area (render as General)",
    )
    backfill_p.add_argument(
        "--news-only",
        action="store_true",
        help="only re-tag cookies with a news source (skip judgments)",
    )
    backfill_p.add_argument(
        "--dry-run", action="store_true", help="show changes without writing them"
    )
    backfill_p.set_defaults(func=_cmd_backfill_areas)

    build_p = sub.add_parser("build", help="render the static site")
    build_p.add_argument("--out", default="./dist", help="output directory (default ./dist)")
    build_p.add_argument(
        "--base-url",
        default=os.environ.get("COOKIES_BASE_URL", "https://cookies.zeeker.sg"),
        help="canonical site base URL (default $COOKIES_BASE_URL or https://cookies.zeeker.sg)",
    )
    build_p.set_defaults(func=_cmd_build)

    backup_p = sub.add_parser(
        "backup", help="snapshot the database and upload to S3 (env: S3_BUCKET, AWS keys)"
    )
    backup_p.set_defaults(func=_cmd_backup)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    return args.func(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
