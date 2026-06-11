"""Shared data model for SG Law Cookies v2 (PRD section 5)."""

from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

ItemType = Literal["news", "judgment"]
Significance = Literal["high", "medium", "low"]
Treatment = Literal["followed", "distinguished", "overruled", "referred"]


def _new_id() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RawItem(BaseModel):
    """Normalised output of the Zeeker ingestion layer."""

    source_url: str
    zeeker_url: str
    title: str
    raw_text: str
    date: date  # document date (e.g. judgment decision_date)
    source_id: str  # Zeeker database, e.g. "sglawwatch"
    item_type: ItemType
    license: str  # underlying source's terms, from Zeeker catalogue metadata


class FolioRef(BaseModel):
    iri: str | None  # None for unresolved entities kept as raw labels
    preferred_label: str
    branch: str
    confidence: float


class TopicExtraction(BaseModel):
    """One extracted legal proposition, pre- and post-FOLIO resolution."""

    headline: str
    summary: str
    why_it_matters: str
    significance: Significance

    # raw LLM output (free text, pre-resolution)
    raw_areas: list[str] = Field(default_factory=list)
    raw_entities: list[str] = Field(default_factory=list)
    raw_concepts: list[str] = Field(default_factory=list)

    # after FOLIO resolution
    folio_areas: list[FolioRef] = Field(default_factory=list)
    folio_entities: list[FolioRef] = Field(default_factory=list)
    folio_concepts: list[FolioRef] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)

    is_duplicate: bool = False
    duplicate_of: str | None = None


class Source(BaseModel):
    id: str = Field(default_factory=_new_id)
    source_url: str  # original document URL — used for all outbound links
    zeeker_url: str  # Zeeker row URL, for attribution/provenance
    title: str
    raw_text: str
    date: date  # document date (e.g. judgment decision_date)
    source_id: str  # Zeeker database, e.g. "sglawwatch", "zeeker-judgements"
    item_type: ItemType
    license: str
    token_count: int
    ingested_at: datetime = Field(default_factory=_utcnow)  # when we saw it in Zeeker


class Cookie(BaseModel):
    id: str = Field(default_factory=_new_id)
    source_ids: list[str] = Field(default_factory=list)  # many-to-many with Source
    headline: str
    summary: str
    why_it_matters: str
    significance: Significance
    folio_areas: list[FolioRef] = Field(default_factory=list)
    folio_entities: list[FolioRef] = Field(default_factory=list)
    folio_concepts: list[FolioRef] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    is_duplicate: bool = False
    duplicate_of: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class JudgmentIssue(BaseModel):
    question: str
    holding: str = ""
    reasoning: str = ""
    folio_concepts: list[FolioRef] = Field(default_factory=list)


class CaseCitation(BaseModel):
    citation: str
    treatment: Treatment
    internal_ref: str | None = None  # link to our own DB if we have this case


class JudgmentMeta(BaseModel):
    source_id: str  # links to Source.id
    citation: str
    court: FolioRef | None = None
    judges: list[str] = Field(default_factory=list)
    parties: list[str] = Field(default_factory=list)
    issues: list[JudgmentIssue] = Field(default_factory=list)
    legislation: list[FolioRef] = Field(default_factory=list)
    cases_cited: list[CaseCitation] = Field(default_factory=list)
    orders: str = ""


class DailyStats(BaseModel):
    date: date
    total_cookies: int = 0
    news_count: int = 0
    judgment_count: int = 0
    high_significance: list[str] = Field(default_factory=list)  # cookie IDs
    medium_significance: list[str] = Field(default_factory=list)
    areas_breakdown: dict[str, int] = Field(default_factory=dict)
    courts_breakdown: dict[str, int] = Field(default_factory=dict)
    busiest_area: str = ""
    unresolved_terms: list[str] = Field(default_factory=list)


class SourceRegistryEntry(BaseModel):
    zeeker_db: str
    table: str
    pipeline: Literal["news", "judgment"]
    license: str
    watermark: str | None = None  # ISO timestamp of last-seen created_at
    active: bool = True
