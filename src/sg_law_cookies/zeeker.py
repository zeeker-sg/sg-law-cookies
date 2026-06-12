"""Zeeker (data.zeeker.sg) Datasette JSON API client — ingestion layer (PRD 3.2, layer 1)."""

import time
from datetime import date, datetime
from typing import Any, NamedTuple

import httpx

from .models import ItemType, RawItem

BASE_URL = "https://data.zeeker.sg"
PAGE_SIZE = 100  # Datasette's default _size ceiling
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Tables routed to the judgment pipeline; everything else is news.
JUDGMENT_TABLES = {"judgments", "enforcement_decisions"}

# Watermark column per (database, table). Most Zeeker tables use
# created_at; sglawwatch and pdpc use imported_on (verified live).
# zeeker-judgements/judgments has no imported_on column and sorts by
# created_at without error (verified live 2026-06-12).
TIMESTAMP_COLUMNS: dict[tuple[str, str], str] = {
    ("sglawwatch", "headlines"): "imported_on",
    ("sglawwatch", "commentaries"): "imported_on",
    ("pdpc", "enforcement_decisions"): "imported_on",
}
DEFAULT_TIMESTAMP_COLUMN = "created_at"

# Max characters of court_summary carried in RawItem.extras.
COURT_SUMMARY_EXTRA_LIMIT = 2000

# Non-content utility tables that survive the prefix/suffix filters.
_EXCLUDED_TABLES = {"schema_versions", "metadata"}


class CatalogueEntry(NamedTuple):
    database: str
    table: str
    license: str
    row_count: int | None


def timestamp_column(db: str, table: str) -> str:
    return TIMESTAMP_COLUMNS.get((db, table), DEFAULT_TIMESTAMP_COLUMN)


def item_type_for(table: str) -> ItemType:
    return "judgment" if table in JUDGMENT_TABLES else "news"


def _is_content_table(name: str, hidden: bool) -> bool:
    if hidden or name in _EXCLUDED_TABLES:
        return False
    if name.startswith("_"):
        return False
    if name.endswith("_fragments") or name.endswith("_fts"):
        return False
    return True


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _set_extra(extras: dict[str, str], key: str, value: Any) -> None:
    """Record a non-empty row value in extras, coerced to str."""
    if value is None or value == "":
        return
    extras[key] = str(value)


def _judgment_text(row: dict[str, Any]) -> tuple[str, str] | None:
    """Best available text for a zeeker-judgements row: (text, source column).

    Prefers full content_text, falling back to the court-issued summary,
    then the AI-generated summary. Returns None when the row carries no
    usable text (e.g. has_content false-ish and no summary) — verified
    live: rows with has_content=0 have content_text = ''.
    """
    for column in ("content_text", "court_summary", "summary"):
        text = row.get(column)
        if text:
            return text, column
    return None


def _row_fields(
    table: str, row: dict[str, Any]
) -> tuple[str, str, str, Any, dict[str, str]] | None:
    """Return (source_url, title, raw_text, raw_document_date, extras) for a row.

    Returns None for judgment-table rows with no usable text — the
    caller should skip those rows entirely.
    """
    if table == "headlines":  # sglawwatch
        return (
            row["source_link"],
            row["title"],
            row.get("text") or row.get("summary") or "",
            row.get("date"),
            {},
        )
    if table == "commentaries":  # sglawwatch
        return (
            row["link"],
            row["title"],
            row.get("full_text") or row.get("description") or "",
            row.get("pub_date"),
            {},
        )
    if table == "judgments":  # zeeker-judgements (columns verified live)
        text_and_source = _judgment_text(row)
        if text_and_source is None:
            return None
        raw_text, text_source = text_and_source
        extras: dict[str, str] = {"text_source": text_source}
        _set_extra(extras, "citation", row.get("citation"))
        _set_extra(extras, "court", row.get("court"))
        _set_extra(extras, "subject_tags", row.get("subject_tags"))
        _set_extra(extras, "pdf_url", row.get("pdf_url"))
        court_summary = row.get("court_summary") or ""
        _set_extra(extras, "court_summary", court_summary[:COURT_SUMMARY_EXTRA_LIMIT])
        return (
            row["source_url"],
            row.get("case_name") or row.get("citation") or "",
            raw_text,
            row.get("decision_date"),
            extras,
        )
    if table == "enforcement_decisions":  # pdpc (columns verified live)
        summary = row.get("summary") or ""
        if not summary:
            return None  # no content_text column; empty summary means no text
        extras = {"text_source": "summary"}
        _set_extra(extras, "organisation", row.get("organisation"))
        _set_extra(extras, "decision_type", row.get("decision_type"))
        _set_extra(extras, "penalty_amount", row.get("penalty_amount"))
        _set_extra(extras, "pdf_url", row.get("pdf_url"))
        return (
            row["decision_url"],
            row["title"],
            summary,
            row.get("decision_date"),
            extras,
        )
    # default: sg-gov-newsrooms *_news shape
    return (
        row["source_url"],
        row["title"],
        row.get("content_text") or row.get("summary") or "",
        row.get("published_date"),
        {},
    )


class ZeekerClient:
    """Read-only client for Zeeker's Datasette JSON API.

    Respects Zeeker's 60/min rate limit via a minimum inter-request
    interval and retries 429/5xx responses with exponential backoff.
    """

    def __init__(
        self,
        base_url: str = BASE_URL,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        backoff: float = 1.0,
        min_interval: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._max_retries = max_retries
        self._backoff = backoff
        self._min_interval = min_interval
        self._last_request = 0.0
        self._licenses: dict[str, str] | None = None

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        for attempt in range(self._max_retries + 1):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_request = time.monotonic()
            response = self._client.get(url, params=params)
            if response.status_code in RETRY_STATUSES and attempt < self._max_retries:
                time.sleep(self._backoff * (2**attempt))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable")

    def licenses(self) -> dict[str, str]:
        """Per-database licence labels from Datasette metadata (cached)."""
        if self._licenses is None:
            metadata = self._get("/-/metadata.json")
            default = metadata.get("license") or "unknown"
            self._licenses = {
                name: db_meta.get("license") or default
                for name, db_meta in metadata.get("databases", {}).items()
                if name != "*"
            }
            self._licenses["*"] = default
        return self._licenses

    def license_for(self, db: str) -> str:
        licenses = self.licenses()
        return licenses.get(db, licenses["*"])

    def discover_catalogue(self) -> list[CatalogueEntry]:
        """Enumerate visible content tables (skips *_fragments, *_fts, utility tables)."""
        databases = self._get("/-/databases.json")
        entries: list[CatalogueEntry] = []
        for db in databases:
            name = db["name"]
            license = self.license_for(name)
            db_info = self._get(f"/{name}.json")
            for table in db_info.get("tables", []):
                if not _is_content_table(table["name"], table.get("hidden", False)):
                    continue
                entries.append(
                    CatalogueEntry(
                        database=name,
                        table=table["name"],
                        license=license,
                        row_count=table.get("count"),
                    )
                )
        return entries

    def fetch_new_rows(
        self,
        db: str,
        table: str,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Rows with watermark column strictly greater than `since`.

        Sorted ascending by the watermark column so the caller can
        advance its watermark monotonically. Follows Datasette keyset
        pagination via the _next token.
        """
        ts_col = timestamp_column(db, table)
        base_params: dict[str, str] = {"_shape": "objects", "_sort": ts_col}
        if since is not None:
            base_params[f"{ts_col}__gt"] = since

        rows: list[dict[str, Any]] = []
        next_token: str | None = None
        try:
            while len(rows) < limit:
                params = dict(base_params)
                params["_size"] = str(min(PAGE_SIZE, limit - len(rows)))
                if next_token is not None:
                    params["_next"] = next_token
                page = self._get(f"/{db}/{table}.json", params=params)
                rows.extend(page.get("rows", []))
                next_token = page.get("next")
                if not next_token:
                    break
        except httpx.HTTPStatusError as exc:
            # Large tables (zeeker-judgements) time out Datasette's SQL
            # limit when filtering/sorting on the unindexed watermark
            # column. rowid IS indexed and correlates with import order,
            # so walk newest-first by rowid and filter client-side.
            if since is None or exc.response.status_code not in (400, 500, 503):
                raise
            return self._fetch_new_rows_by_rowid(db, table, ts_col, since, limit)
        return rows[:limit]

    # Safety cap for the rowid fallback: stop walking after this many rows
    # even if we haven't reached the watermark (protects against a table
    # whose rowid order doesn't correlate with the watermark column).
    _ROWID_WALK_CAP = 2000

    def _fetch_new_rows_by_rowid(
        self,
        db: str,
        table: str,
        ts_col: str,
        since: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        next_token: str | None = None
        walked = 0
        while walked < self._ROWID_WALK_CAP:
            params: dict[str, str] = {
                "_shape": "objects",
                "_sort_desc": "rowid",
                "_size": str(PAGE_SIZE),
            }
            if next_token is not None:
                params["_next"] = next_token
            page = self._get(f"/{db}/{table}.json", params=params)
            page_rows = page.get("rows", [])
            if not page_rows:
                break
            walked += len(page_rows)
            hit_watermark = False
            for row in page_rows:
                ts = row.get(ts_col)
                if ts is not None and str(ts) <= since:
                    hit_watermark = True
                    break
                collected.append(row)
            next_token = page.get("next")
            if hit_watermark or not next_token:
                break
        collected.sort(key=lambda r: str(r.get(ts_col) or ""))
        return collected[:limit]

    def to_raw_item(self, db: str, table: str, row: dict[str, Any]) -> RawItem | None:
        """Map a Zeeker row to a RawItem (source_url = original document URL).

        Returns None for judgment-table rows that carry no usable text
        (has_content false-ish and no court/AI summary) — callers must
        skip those rows.
        """
        fields = _row_fields(table, row)
        if fields is None:
            return None
        source_url, title, raw_text, raw_date, extras = fields
        doc_date = _parse_date(raw_date) or _parse_date(row.get(timestamp_column(db, table)))
        if doc_date is None:
            raise ValueError(f"no usable date in {db}/{table} row {row.get('id')!r}")
        row_id = row.get("id")
        zeeker_url = (
            f"{self.base_url}/{db}/{table}/{row_id}"
            if row_id is not None
            else f"{self.base_url}/{db}/{table}"
        )
        return RawItem(
            source_url=source_url,
            zeeker_url=zeeker_url,
            title=title,
            raw_text=raw_text,
            date=doc_date,
            source_id=db,
            item_type=item_type_for(table),
            license=self.license_for(db),
            extras=extras,
        )
