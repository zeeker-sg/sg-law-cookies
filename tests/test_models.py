from datetime import date, datetime, timezone

from sg_law_cookies import (
    CaseCitation,
    Cookie,
    DailyStats,
    FolioRef,
    JudgmentIssue,
    JudgmentMeta,
    RawItem,
    Source,
    SourceRegistryEntry,
    TopicExtraction,
)


def roundtrip(model):
    return type(model).model_validate(model.model_dump())


def test_raw_item_roundtrip():
    item = RawItem(
        source_url="https://www.judiciary.gov.sg/judgments/2026-sghc-34",
        zeeker_url="https://data.zeeker.sg/zeeker-judgements/judgments/1",
        title="Tan v Lim",
        raw_text="The plaintiff claims...",
        date=date(2026, 6, 1),
        source_id="zeeker-judgements",
        item_type="judgment",
        license="CC-BY 4.0",
    )
    assert roundtrip(item) == item


def test_folio_ref_unresolved_iri_none():
    ref = FolioRef(iri=None, preferred_label="PDPC", branch="unresolved", confidence=0.0)
    assert roundtrip(ref) == ref
    assert ref.iri is None


def test_topic_extraction_defaults_and_roundtrip():
    topic = TopicExtraction(
        headline="EP salary floor rises to SGD 6,000",
        summary="MOM announced a higher Employment Pass salary floor.",
        why_it_matters="Employers must budget for higher EP costs from 2027.",
        significance="high",
        raw_areas=["Employment"],
        raw_entities=["Ministry of Manpower"],
        raw_concepts=["work passes"],
    )
    assert topic.folio_areas == []
    assert topic.unresolved == []
    assert topic.is_duplicate is False
    assert topic.duplicate_of is None
    assert roundtrip(topic) == topic


def test_source_defaults():
    src = Source(
        source_url="https://www.straitstimes.com/article",
        zeeker_url="https://data.zeeker.sg/sglawwatch/headlines/9",
        title="Headline",
        raw_text="Body text",
        date=date(2026, 6, 10),
        source_id="sglawwatch",
        item_type="news",
        license="CC-BY 4.0",
        token_count=420,
    )
    assert len(src.id) == 36
    assert src.ingested_at.tzinfo is not None
    assert roundtrip(src) == src


def test_cookie_many_to_many_sources():
    ref = FolioRef(
        iri="https://openlegalstandard.org/ontology/employment-law",
        preferred_label="Employment Law",
        branch="areas_of_law",
        confidence=1.0,
    )
    cookie = Cookie(
        source_ids=["src-1", "src-2"],
        headline="CA distinguishes prior authority on abuse of process",
        summary="The Court of Appeal held...",
        why_it_matters="Arbitral findings can ground abuse-of-process arguments.",
        significance="medium",
        folio_areas=[ref],
        created_at=datetime(2026, 6, 11, 8, 0, tzinfo=timezone.utc),
    )
    assert cookie.source_ids == ["src-1", "src-2"]
    assert cookie.is_duplicate is False
    assert roundtrip(cookie) == cookie


def test_judgment_meta_roundtrip():
    meta = JudgmentMeta(
        source_id="src-1",
        citation="[2026] SGHC 34",
        court=FolioRef(
            iri="https://openlegalstandard.org/ontology/high-court",
            preferred_label="High Court",
            branch="forum_venues",
            confidence=0.9,
        ),
        judges=["Tan J"],
        parties=["Tan", "Lim"],
        issues=[
            JudgmentIssue(
                question="Whether the defendant owed a duty of care",
                holding="No duty arose",
                reasoning="The relationship lacked proximity.",
            )
        ],
        cases_cited=[CaseCitation(citation="[2013] SGCA 36", treatment="followed")],
        orders="Claim dismissed.",
    )
    assert meta.cases_cited[0].internal_ref is None
    assert roundtrip(meta) == meta


def test_daily_stats_defaults():
    stats = DailyStats(date=date(2026, 6, 11))
    assert stats.total_cookies == 0
    assert stats.areas_breakdown == {}
    full = DailyStats(
        date=date(2026, 6, 11),
        total_cookies=87,
        news_count=15,
        judgment_count=72,
        high_significance=["c-1"],
        medium_significance=["c-2"],
        areas_breakdown={"Employment Law": 23},
        courts_breakdown={"High Court": 40},
        busiest_area="Employment Law",
        unresolved_terms=["PDPC"],
    )
    assert roundtrip(full) == full


def test_source_registry_entry():
    entry = SourceRegistryEntry(
        zeeker_db="sglawwatch",
        table="headlines",
        pipeline="news",
        license="CC-BY 4.0",
    )
    assert entry.watermark is None
    assert entry.active is True
    entry2 = entry.model_copy(update={"watermark": "2026-06-10T23:59:00+00:00"})
    assert roundtrip(entry2) == entry2
