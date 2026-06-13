"""Counter Map data layer (PRD section 7): per-day sky JSON for the constellation.

Emits the dicts that sitegen serialises to dist/data/sky/<date>.json and
dist/data/sky/index.json. Filtering matches sitegen.build_daily_context
exactly (duplicate-flagged cookies are skipped) so the counter map and the
daily pages never disagree on counts.
"""

import hashlib
import sqlite3
from datetime import date, timedelta

from sg_law_cookies import db
from sg_law_cookies.models import Cookie, Source
from sg_law_cookies.sg_mappings import SG_LOCAL_BRANCH

HEADLINE_MAX = 90
TIE_WINDOW_DAYS = 7
ISSUES_MAX = 8

_SIG_RANK = {"high": 0, "medium": 1, "low": 2}


# ── helpers ──────────────────────────────────────────────────────────


def _day_cookies(conn: sqlite3.Connection, day: date) -> list[Cookie]:
    """Cookies for the day with sitegen's exact duplicate filtering."""
    return [c for c in db.cookies_for_date(conn, day) if not c.is_duplicate]


def _area_labels(cookie: Cookie) -> list[str]:
    """Distinct non-empty folio_area labels, in stored order (mirrors sitegen)."""
    seen: list[str] = []
    for ref in cookie.folio_areas:
        if ref.preferred_label and ref.preferred_label not in seen:
            seen.append(ref.preferred_label)
    return seen


def _primary_area(cookie: Cookie) -> str:
    """First folio_area preferred_label, fallback "General"."""
    labels = _area_labels(cookie)
    return labels[0] if labels else "General"


def _kind(sources: list[Source]) -> str:
    """Kind from the cookie's first source item_type; sourceless -> news."""
    return sources[0].item_type if sources else "news"


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


# ── v2 cookie payload helpers ────────────────────────────────────────


def _cookie_hash(cookie_id: str) -> str:
    """Stable public id: sha256 of the cookie id, first 12 hex chars
    (zeeker-publish convention)."""
    return hashlib.sha256(cookie_id.encode("utf-8")).hexdigest()[:12]


def _concept_labels(cookie: Cookie) -> list[str]:
    """Distinct resolved FOLIO concept labels, in stored order.

    Resolved means iri is not null OR branch is sg_local; unresolved
    placeholder refs (iri=None, other branches) are excluded.
    """
    seen: list[str] = []
    for ref in cookie.folio_concepts:
        if not ref.preferred_label:
            continue
        if ref.iri is None and ref.branch != SG_LOCAL_BRANCH:
            continue
        if ref.preferred_label not in seen:
            seen.append(ref.preferred_label)
    return seen


def _url_and_src(
    conn: sqlite3.Connection, sources: list[Source], day: date
) -> tuple[str, str]:
    """(url, src) from the first source; sourceless mirrors sitegen's
    day-page fallback (url=/d/<date>/, label COOKIES)."""
    # Lazy import: sitegen imports skydata at module level.
    from sg_law_cookies.sitegen import source_label

    if not sources:
        return f"/d/{day.isoformat()}/", "COOKIES"
    first = sources[0]
    src = source_label(first)
    if first.item_type == "judgment":
        meta = db.get_judgment_meta(conn, first.id)
        if meta is not None and meta.citation:
            src = meta.citation
    return first.source_url, src


def _judgment_issues(conn: sqlite3.Connection, sources: list[Source]) -> list[dict]:
    """Issue dicts (q + hold) from judgment_meta on the first judgment-typed
    source. Empty holdings omit the "hold" key; capped at ISSUES_MAX."""
    for source in sources:
        if source.item_type != "judgment":
            continue
        meta = db.get_judgment_meta(conn, source.id)
        if meta is None:
            return []
        issues = []
        for issue in meta.issues[:ISSUES_MAX]:
            entry = {"q": issue.question}
            if issue.holding:
                entry["hold"] = issue.holding
            issues.append(entry)
        return issues
    return []


# ── ties (trailing 7-day window ending at day D) ─────────────────────


def _ties_for_day(
    conn: sqlite3.Connection, day: date, present_areas: set[str]
) -> list[dict]:
    """Doctrine-thread weights between area pairs, week-scoped.

    Over the TRAILING 7 days ending at `day`: +2 per cookie whose
    folio_areas span both areas, +1 per FOLIO concept label appearing in
    cookies whose primary areas differ. A pair is included only if both
    areas are present on `day` itself.
    """
    if len(present_areas) < 2:
        return []

    weights: dict[tuple[str, str], int] = {}
    concept_areas: dict[str, set[str]] = {}

    for offset in range(TIE_WINDOW_DAYS):
        window_day = day - timedelta(days=offset)
        for cookie in _day_cookies(conn, window_day):
            labels = _area_labels(cookie)
            for i in range(len(labels)):
                for j in range(i + 1, len(labels)):
                    key = _pair(labels[i], labels[j])
                    weights[key] = weights.get(key, 0) + 2
            primary = _primary_area(cookie)
            for ref in cookie.folio_concepts:
                if ref.preferred_label:
                    concept_areas.setdefault(ref.preferred_label, set()).add(primary)

    for areas in concept_areas.values():
        labels = sorted(areas)
        for i in range(len(labels)):
            for j in range(i + 1, len(labels)):
                key = (labels[i], labels[j])
                weights[key] = weights.get(key, 0) + 1

    ties = [
        {"a": a, "b": b, "w": w}
        for (a, b), w in weights.items()
        if a in present_areas and b in present_areas
    ]
    ties.sort(key=lambda t: (-t["w"], t["a"], t["b"]))
    return ties


# ── public builders ──────────────────────────────────────────────────


def build_sky_day(conn: sqlite3.Connection, day: date) -> dict:
    """Per-day sky JSON dict per the contract (dist/data/sky/<date>.json)."""
    cookies = _day_cookies(conn, day)
    sources_map = db.sources_for_cookies(conn, [c.id for c in cookies])

    grouped: dict[str, list[tuple[int, int, dict, str]]] = {}
    for idx, cookie in enumerate(cookies):
        srcs = sources_map.get(cookie.id, [])
        kind = _kind(srcs)
        url, src = _url_and_src(conn, srcs, day)
        entry = {
            "id": _cookie_hash(cookie.id),
            "h": cookie.headline[:HEADLINE_MAX],
            "headline": cookie.headline,
            "summary": cookie.summary,
            "why": cookie.why_it_matters,
            "sig": cookie.significance,
            "kind": kind,
            "url": url,
            "src": src,
            "concepts": _concept_labels(cookie),
        }
        if kind == "judgment":
            entry["issues"] = _judgment_issues(conn, srcs)
        grouped.setdefault(_primary_area(cookie), []).append(
            (_SIG_RANK[cookie.significance], idx, entry, cookie.significance)
        )

    areas = []
    for label, items in grouped.items():
        items.sort(key=lambda item: (item[0], item[1]))
        areas.append(
            {
                "label": label,
                "count": len(items),
                "high": sum(1 for item in items if item[3] == "high"),
                "cookies": [item[2] for item in items],
            }
        )
    areas.sort(key=lambda a: (-a["count"], a["label"]))

    kinds = [item[2]["kind"] for items in grouped.values() for item in items]
    totals = {
        "total": len(cookies),
        "news": sum(1 for k in kinds if k == "news"),
        "judgments": sum(1 for k in kinds if k == "judgment"),
        "high": sum(1 for c in cookies if c.significance == "high"),
    }

    return {
        "date": day.isoformat(),
        "totals": totals,
        "areas": areas,
        "ties": _ties_for_day(conn, day, {a["label"] for a in areas}),
    }


def build_sky_index(conn: sqlite3.Connection) -> dict:
    """Sky index dict per the contract (dist/data/sky/index.json), ascending."""
    days = []
    for day_iso in reversed(db.list_cookie_dates(conn)):  # newest-first -> asc
        cookies = _day_cookies(conn, date.fromisoformat(day_iso))
        days.append(
            {
                "date": day_iso,
                "total": len(cookies),
                "high": sum(1 for c in cookies if c.significance == "high"),
            }
        )
    return {"days": days}
