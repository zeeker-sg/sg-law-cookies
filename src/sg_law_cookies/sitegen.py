"""Static site generator: bakery shopfront pages from the cookie DB.

Builds the daily pages, archive, about page, Atom feed and static CSS
into an output directory. Every "read the source" link uses the cookie's
ORIGINAL source_url (never a Zeeker URL) and every page carries the
data.zeeker.sg attribution + disclaimer via base.html.j2 (PRD §2.5, §6.1).
"""

import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from sg_law_cookies import db
from sg_law_cookies.feed import build_feed, render_feed
from sg_law_cookies.models import Cookie, Source

_PACKAGE_DIR = Path(__file__).parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"

SGT = timezone(timedelta(hours=8))
TRAY_CAP = 14

# zeeker-judgements titles that look like neutral citations, e.g. "[2026] SGHC 102"
_CITATION_RE = re.compile(r"\[\d{4}\]\s+[A-Z][A-Za-z()]*\s+\d+")


@dataclass
class BuildReport:
    pages: int = 0
    dates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── context helpers ──────────────────────────────────────────────────


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        # .j2 is not in jinja's default autoescape list; templates rely on it.
        autoescape=select_autoescape(
            enabled_extensions=("html", "j2", "xml"), default_for_string=True
        ),
        keep_trailing_newline=True,
    )


def source_label(source: Source) -> str:
    """Short display label for a source (mockup price-card style)."""
    sid = source.source_id
    if sid == "sglawwatch":
        return "SLW"
    if sid == "pdpc":
        return "PDPC"
    if sid == "zeeker-judgements":
        match = _CITATION_RE.search(source.title)
        return match.group(0) if match else "JUDICIARY"
    if sid == "sg-gov-newsrooms":
        # zeeker_url is {base}/{db}/{table}/{row_id}; agency acronym is the
        # table prefix, e.g. "mom_news" -> "MOM".
        parts = [p for p in urlsplit(source.zeeker_url).path.split("/") if p]
        if len(parts) >= 2:
            prefix = parts[1].split("_", 1)[0]
            if prefix:
                return prefix.upper()
        return "GOV"
    return sid.upper()


def _kind(sources: list[Source]) -> str:
    """Cookie kind from its first source; judgments win over mixed bags."""
    if any(s.item_type == "judgment" for s in sources):
        return "judgment"
    return "news"


def _to_sgt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SGT)


def _date_display(day: date) -> str:
    return day.strftime("%a %d %b %Y").upper()


def _date_long(day: date) -> str:
    return f"{day.strftime('%A')}, {day.day} {day.strftime('%B %Y')}"


def _area_labels(cookie: Cookie) -> list[str]:
    seen: list[str] = []
    for ref in cookie.folio_areas:
        if ref.preferred_label and ref.preferred_label not in seen:
            seen.append(ref.preferred_label)
    return seen


def _empty_context(day: date, *, is_today: bool) -> dict:
    return {
        "date": day.isoformat(),
        "date_display": _date_display(day),
        "date_long": _date_long(day),
        "oven_time": None,
        "totals": {
            "total": 0,
            "news": 0,
            "judgments": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
        },
        "busiest_area": None,
        "new_ingredient": None,
        "areas_ticker": [],
        "tray": [],
        "specials": [],
        "menu": [],
        "rack": {"count": 0, "breakdown": None},
        "prev_date": None,
        "next_date": None,
        "is_today": is_today,
    }


def build_daily_context(
    conn: sqlite3.Connection,
    day: date,
    *,
    prev_date: str | None = None,
    next_date: str | None = None,
    is_today: bool = False,
    base_url: str = "",
    warnings: list[str] | None = None,
) -> dict:
    """Assemble the daily-page template context per the contract."""
    base = base_url.rstrip("/")
    cookies = [c for c in db.cookies_for_date(conn, day) if not c.is_duplicate]
    sources_map = db.sources_for_cookies(conn, [c.id for c in cookies])

    ctx = _empty_context(day, is_today=is_today)
    ctx["prev_date"] = prev_date
    ctx["next_date"] = next_date
    if not cookies:
        return ctx

    # per-cookie derived bits
    derived: list[dict] = []
    area_counts: dict[str, int] = {}
    for cookie in cookies:
        srcs = sources_map.get(cookie.id, [])
        if srcs:
            url = srcs[0].source_url  # ORIGINAL document URL (PRD §2.5)
            label = source_label(srcs[0])
        else:
            url = f"{base}/d/{day.isoformat()}/" if base else f"/d/{day.isoformat()}/"
            label = "COOKIES"
            if warnings is not None:
                warnings.append(
                    f"cookie {cookie.id} ({day.isoformat()}) has no sources; "
                    "linked to the day page instead"
                )
        areas = _area_labels(cookie)
        for area in areas:
            area_counts[area] = area_counts.get(area, 0) + 1
        derived.append(
            {
                "cookie": cookie,
                "kind": _kind(srcs),
                "area": areas[0] if areas else None,
                "source_url": url,
                "source_label": label,
                "chips": max(1, min(5, len(cookie.folio_concepts))),
            }
        )

    sig_rank = {"high": 0, "medium": 1, "low": 2}
    derived.sort(key=lambda d: sig_rank[d["cookie"].significance])

    ctx["totals"] = {
        "total": len(cookies),
        "news": sum(1 for d in derived if d["kind"] == "news"),
        "judgments": sum(1 for d in derived if d["kind"] == "judgment"),
        "high": sum(1 for c in cookies if c.significance == "high"),
        "medium": sum(1 for c in cookies if c.significance == "medium"),
        "low": sum(1 for c in cookies if c.significance == "low"),
    }

    ctx["oven_time"] = (
        _to_sgt(max(c.created_at for c in cookies)).strftime("%H:%M") + " SGT"
    )

    ranked_areas = sorted(area_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ctx["areas_ticker"] = [
        {"label": label.upper(), "count": count} for label, count in ranked_areas
    ]
    ctx["busiest_area"] = ranked_areas[0][0] if ranked_areas else None

    terms = db.latest_unresolved_terms(conn, day)
    if not terms:
        terms = [t for c in cookies for t in c.unresolved]
    ctx["new_ingredient"] = terms[0] if terms else None

    ctx["tray"] = [
        {
            "id": d["cookie"].id,
            "headline": d["cookie"].headline,
            "significance": d["cookie"].significance,
            "kind": d["kind"],
            "area": d["area"],
            "source_label": d["source_label"],
            "source_url": d["source_url"],
            "chips": d["chips"],
        }
        for d in derived[:TRAY_CAP]
    ]

    ctx["specials"] = [
        {
            "headline": d["cookie"].headline,
            "summary": d["cookie"].summary,
            "why_it_matters": d["cookie"].why_it_matters,
            "kind": d["kind"],
            "source_label": d["source_label"],
            "source_url": d["source_url"],
        }
        for d in derived
        if d["cookie"].significance == "high"
    ]

    # mediums grouped by first folio area (fallback "General")
    groups: dict[str, list[dict]] = {}
    for d in derived:
        if d["cookie"].significance != "medium":
            continue
        area = d["area"] or "General"
        groups.setdefault(area, []).append(
            {
                "headline": d["cookie"].headline,
                "kind": d["kind"],
                "source_url": d["source_url"],
                "src_short": d["source_label"],
            }
        )
    ctx["menu"] = [
        {"area": area, "items": items}
        for area, items in sorted(
            groups.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )
    ]

    # lows -> cooling rack with area breakdown
    lows = [d for d in derived if d["cookie"].significance == "low"]
    low_areas: dict[str, int] = {}
    for d in lows:
        key = (d["area"] or "general").lower()
        low_areas[key] = low_areas.get(key, 0) + 1
    breakdown = (
        ", ".join(
            f"{area} ({count})"
            for area, count in sorted(low_areas.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        or None
    )
    ctx["rack"] = {"count": len(lows), "breakdown": breakdown}
    return ctx


# ── build ────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_site(conn: sqlite3.Connection, out_dir: Path, base_url: str) -> BuildReport:
    """Render the full static site into out_dir."""
    report = BuildReport()
    env = _jinja_env()
    daily_tpl = env.get_template("daily.html.j2")
    about_tpl = env.get_template("about.html.j2")
    archive_tpl = env.get_template("archive.html.j2")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    day_isos = db.list_cookie_dates(conn)  # newest first
    report.dates = list(day_isos)
    today_iso = date.today().isoformat()

    archive_rows: list[dict] = []
    for i, day_iso in enumerate(day_isos):
        day = date.fromisoformat(day_iso)
        prev_iso = day_isos[i + 1] if i + 1 < len(day_isos) else None
        next_iso = day_isos[i - 1] if i > 0 else None
        ctx = build_daily_context(
            conn,
            day,
            prev_date=prev_iso,
            next_date=next_iso,
            is_today=(day_iso == today_iso),
            base_url=base_url,
            warnings=report.warnings,
        )
        _write(out_dir / "d" / day_iso / "index.html", daily_tpl.render(**ctx))
        report.pages += 1
        archive_rows.append(
            {
                "date": day_iso,
                "date_long": _date_long(day),
                "total": ctx["totals"]["total"],
                "high": ctx["totals"]["high"],
            }
        )
        if i == 0:
            # homepage is the latest day
            home_ctx = dict(ctx, is_today=True)
            _write(out_dir / "index.html", daily_tpl.render(**home_ctx))
            report.pages += 1

    if not day_isos:
        # empty DB: oven's-warming-up homepage rather than a crash
        ctx = _empty_context(date.today(), is_today=True)
        _write(out_dir / "index.html", daily_tpl.render(**ctx))
        report.pages += 1
        report.warnings.append("no cookies in database; built empty-state homepage")

    _write(out_dir / "d" / "index.html", archive_tpl.render(days=archive_rows))
    report.pages += 1

    _write(out_dir / "about" / "index.html", about_tpl.render())
    report.pages += 1

    _write(out_dir / "feed.xml", render_feed(build_feed(conn, base_url)))
    report.pages += 1

    (out_dir / "static").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_STATIC_DIR / "site.css", out_dir / "static" / "site.css")

    return report
