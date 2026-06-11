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
TIMESTAMP_COLUMNS: dict[tuple[str, str], str] = {
    ("sglawwatch", "headlines"): "imported_on",
    ("sglawwatch", "commentaries"): "imported_on",
    ("pdpc", "enforcement_decisions"): "imported_on",
}
DEFAULT_TIMESTAMP_COLUMN = "created_at"

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


def _row_fields(table: str, row: dict[str, Any]) -> tuple[str, str, str, Any]:
    """Return (source_url, title, raw_text, raw_document_date) for a row."""
    if table == "headlines":  # sglawwatch
        return (
            row["source_link"],
            row["title"],
            row.get("text") or row.get("summary") or "",
            row.get("date"),
        )
    if table == "commentaries":  # sglawwatch
        return (
            row["link"],
            row["title"],
            row.get("full_text") or row.get("description") or "",
            row.get("pub_date"),
        )
    if table == "judgments":  # zeeker-judgements
        return (
            row["source_url"],
            row.get("case_name") or row.get("citation") or "",
            row.get("content_text") or row.get("court_summary") or row.get("summary") or "",
            row.get("decision_date"),
        )
    if table == "enforcement_decisions":  # pdpc
        return (
            row["decision_url"],
            row["title"],
            row.get("summary") or "",
            row.get("decision_date"),
        )
    # default: sg-gov-newsrooms *_news shape
    return (
        row["source_url"],
        row["title"],
        row.get("content_text") or row.get("summary") or "",
        row.get("published_date"),
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
        return rows[:limit]

    def to_raw_item(self, db: str, table: str, row: dict[str, Any]) -> RawItem:
        """Map a Zeeker row to a RawItem (source_url = original document URL)."""
        source_url, title, raw_text, raw_date = _row_fields(table, row)
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
        )
