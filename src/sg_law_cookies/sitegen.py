"""Static site generator: bakery shopfront pages from the cookie DB.

Builds the daily pages, archive, about page, Atom feed and static CSS
into an output directory. Every "read the source" link uses the cookie's
ORIGINAL source_url (never a Zeeker URL) and every page carries the
data.zeeker.sg attribution + disclaimer via base.html.j2 (PRD §2.5, §6.1).
"""

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from sg_law_cookies import db, skydata
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


def _monday_of(day: date) -> date:
    """The Monday that begins `day`'s ISO week."""
    return day - timedelta(days=day.weekday())


def _iso_week_key(day: date) -> str:
    """URL/key for a week, e.g. '2026-W25' (the year is the ISO year)."""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _utc_day(dt: datetime) -> date:
    """The UTC calendar date of a timestamp (matches SQL date(created_at))."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date()


def _pub_day(cookie: Cookie, srcs: list[Source]) -> date:
    """The cookie's publication date: earliest source document date, falling
    back to its processing date. Mirrors db._PUB_DATE_SQL so a cookie buckets
    into the same day the daily/weekly queries placed it."""
    dates = [s.date for s in srcs if s.date]
    return min(dates) if dates else _utc_day(cookie.created_at)


def _week_range_display(monday: date) -> str:
    """Compact Mon–Sun span, e.g. '30 JUN – 6 JUL 2026' or '1–7 JUN 2026'."""
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.day}–{sunday.day} {sunday.strftime('%b %Y').upper()}"
    if monday.year == sunday.year:
        return (
            f"{monday.day} {monday.strftime('%b').upper()} – "
            f"{sunday.day} {sunday.strftime('%b %Y').upper()}"
        )
    return (
        f"{monday.day} {monday.strftime('%b %Y').upper()} – "
        f"{sunday.day} {sunday.strftime('%b %Y').upper()}"
    )


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
    ctx["week_url"] = f"/weekly/{_iso_week_key(_monday_of(day))}/"
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
            url = (
                f"{base}/daily/{day.isoformat()}/"
                if base
                else f"/daily/{day.isoformat()}/"
            )
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


def build_weekly_context(
    conn: sqlite3.Connection,
    monday: date,
    *,
    prev_week: str | None = None,
    next_week: str | None = None,
    base_url: str = "",
    warnings: list[str] | None = None,
) -> dict:
    """Assemble the weekly-bake page context: the week's patterns, not per-cookie.

    Aggregates non-duplicate cookies across the 7-day week beginning `monday`
    into headline totals, the week-in-days strip, the high-significance
    specials reel, and the recurring areas/doctrine that ran through the week.
    """
    base = base_url.rstrip("/")
    iso = monday.isocalendar()
    cookies = [c for c in db.cookies_for_week(conn, monday) if not c.is_duplicate]
    sources_map = db.sources_for_cookies(conn, [c.id for c in cookies])

    day_list = [monday + timedelta(days=i) for i in range(7)]
    day_counts: dict[date, dict[str, int]] = {
        d: {"total": 0, "high": 0} for d in day_list
    }

    ctx: dict = {
        "week_key": _iso_week_key(monday),
        "week_label": f"W{iso.week:02d}",
        "week_num": iso.week,
        "year": iso.year,
        "range_display": _week_range_display(monday),
        "prev_week": prev_week,
        "next_week": next_week,
        "totals": {"total": 0, "news": 0, "judgments": 0, "high": 0, "medium": 0, "low": 0},
        "days_baked": 0,
        "busiest_area": None,
        "new_ingredient": None,
        "areas_ticker": [],
        "specials": [],
        "specials_extra": 0,
        "ovens": [],
        "doctrine": [],
        "days": [],
    }

    def _days_strip() -> list[dict]:
        return [
            {
                "date": d.isoformat(),
                "dow": d.strftime("%a").upper(),
                "day": d.day,
                "total": day_counts[d]["total"],
                "high": day_counts[d]["high"],
                "rested": day_counts[d]["total"] == 0,
            }
            for d in day_list
        ]

    if not cookies:
        ctx["days"] = _days_strip()
        return ctx

    derived: list[dict] = []
    area_counts: dict[str, int] = {}
    concept_counts: dict[str, int] = {}
    ingredients: list[str] = []
    for cookie in cookies:
        srcs = sources_map.get(cookie.id, [])
        cday = _pub_day(cookie, srcs)
        if srcs:
            url = srcs[0].source_url  # ORIGINAL document URL (PRD §2.5)
            label = source_label(srcs[0])
        else:
            cday0 = cday.isoformat()
            url = f"{base}/daily/{cday0}/" if base else f"/daily/{cday0}/"
            label = "COOKIES"
            if warnings is not None:
                warnings.append(
                    f"cookie {cookie.id} ({cday0}) has no sources; "
                    "linked to its day page instead"
                )
        areas = _area_labels(cookie)
        for area in areas:
            area_counts[area] = area_counts.get(area, 0) + 1
        seen_concepts: list[str] = []
        for ref in cookie.folio_concepts:
            if ref.preferred_label and ref.preferred_label not in seen_concepts:
                seen_concepts.append(ref.preferred_label)
        for concept in seen_concepts:
            concept_counts[concept] = concept_counts.get(concept, 0) + 1
        for term in cookie.unresolved:
            if term not in ingredients:
                ingredients.append(term)
        if cday in day_counts:
            day_counts[cday]["total"] += 1
            if cookie.significance == "high":
                day_counts[cday]["high"] += 1
        derived.append(
            {
                "cookie": cookie,
                "kind": _kind(srcs),
                "area": areas[0] if areas else None,
                "source_url": url,
                "source_label": label,
                "day": cday,
            }
        )

    ctx["totals"] = {
        "total": len(cookies),
        "news": sum(1 for d in derived if d["kind"] == "news"),
        "judgments": sum(1 for d in derived if d["kind"] == "judgment"),
        "high": sum(1 for c in cookies if c.significance == "high"),
        "medium": sum(1 for c in cookies if c.significance == "medium"),
        "low": sum(1 for c in cookies if c.significance == "low"),
    }
    ctx["days_baked"] = sum(1 for v in day_counts.values() if v["total"] > 0)

    ranked_areas = sorted(area_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ctx["areas_ticker"] = [
        {"label": label.upper(), "count": count} for label, count in ranked_areas
    ]
    ctx["busiest_area"] = ranked_areas[0][0] if ranked_areas else None
    ctx["ovens"] = [{"label": label, "count": count} for label, count in ranked_areas[:12]]

    recurring = sorted(
        ((label, n) for label, n in concept_counts.items() if n >= 2),
        key=lambda kv: (-kv[1], kv[0]),
    )
    ctx["doctrine"] = [{"label": label, "count": n} for label, n in recurring[:8]]
    ctx["new_ingredient"] = ingredients[0] if ingredients else None

    SPECIALS_CAP = 12
    highs = [d for d in derived if d["cookie"].significance == "high"]
    highs.sort(key=lambda d: (d["day"], d["cookie"].headline))
    ctx["specials"] = [
        {
            "headline": d["cookie"].headline,
            "summary": d["cookie"].summary,
            "why_it_matters": d["cookie"].why_it_matters,
            "kind": d["kind"],
            "source_label": d["source_label"],
            "source_url": d["source_url"],
            "day_label": d["day"].strftime("%a").upper(),
        }
        for d in highs[:SPECIALS_CAP]
    ]
    ctx["specials_extra"] = max(0, len(highs) - SPECIALS_CAP)
    ctx["days"] = _days_strip()
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
    counter_map_tpl = env.get_template("counter_map.html.j2")
    weekly_tpl = env.get_template("weekly.html.j2")
    weekly_index_tpl = env.get_template("weekly_index.html.j2")

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
        _write(out_dir / "daily" / day_iso / "index.html", daily_tpl.render(**ctx))
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

    _write(out_dir / "daily" / "index.html", archive_tpl.render(days=archive_rows))
    report.pages += 1

    # weekly bakes: one page per ISO week that has cookies, newest first
    weeks: dict[str, date] = {}
    for day_iso in day_isos:
        monday = _monday_of(date.fromisoformat(day_iso))
        weeks[_iso_week_key(monday)] = monday
    ordered_weeks = sorted(weeks.items(), key=lambda kv: kv[1], reverse=True)
    week_index_rows: list[dict] = []
    for i, (week_key, monday) in enumerate(ordered_weeks):
        prev_key = ordered_weeks[i + 1][0] if i + 1 < len(ordered_weeks) else None
        next_key = ordered_weeks[i - 1][0] if i > 0 else None
        wctx = build_weekly_context(
            conn,
            monday,
            prev_week=prev_key,
            next_week=next_key,
            base_url=base_url,
            warnings=report.warnings,
        )
        _write(out_dir / "weekly" / week_key / "index.html", weekly_tpl.render(**wctx))
        report.pages += 1
        week_index_rows.append(
            {
                "key": week_key,
                "label": wctx["week_label"],
                "year": wctx["year"],
                "range_display": wctx["range_display"],
                "total": wctx["totals"]["total"],
                "high": wctx["totals"]["high"],
                "days_baked": wctx["days_baked"],
            }
        )
    _write(out_dir / "weekly" / "index.html", weekly_index_tpl.render(weeks=week_index_rows))
    report.pages += 1

    _write(out_dir / "about" / "index.html", about_tpl.render())
    report.pages += 1

    _write(out_dir / "feed.xml", render_feed(build_feed(conn, base_url)))
    report.pages += 1

    # counter map (PRD §7): page + per-day sky JSON the page fetches
    _write(out_dir / "counter-map" / "index.html", counter_map_tpl.render())
    report.pages += 1
    _write(
        out_dir / "data" / "sky" / "index.json",
        json.dumps(skydata.build_sky_index(conn), ensure_ascii=False),
    )
    for day_iso in day_isos:
        _write(
            out_dir / "data" / "sky" / f"{day_iso}.json",
            json.dumps(
                skydata.build_sky_day(conn, date.fromisoformat(day_iso)),
                ensure_ascii=False,
            ),
        )

    (out_dir / "static").mkdir(parents=True, exist_ok=True)
    for asset in ("site.css", "sky.css", "sky.js", "d3.v7.min.js"):
        shutil.copyfile(_STATIC_DIR / asset, out_dir / "static" / asset)

    return report
