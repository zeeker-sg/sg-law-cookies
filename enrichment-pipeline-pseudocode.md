# SG Law Cookies — Enrichment Pipeline Pseudocode

## Overview

Two pipelines feed into one shared database. Both end with a FOLIO
resolution step that normalizes free-text labels into standardized
ontology concepts.

```
  ┌─────────────┐     ┌──────────────┐
  │ News Source  │     │ eLitigation  │
  └──────┬──────┘     └──────┬───────┘
         │                   │
    ┌────▼────┐        ┌─────▼─────┐
    │  News   │        │ Judgment   │
    │ Pipeline│        │ Pipeline   │
    └────┬────┘        └─────┬─────┘
         │                   │
         │    ┌──────────┐   │
         └───►│  Shared  │◄──┘
              │ Database │
              └──────────┘
```

---

## 1. Common Types

```
RawItem:
    source_url:  str
    title:       str
    raw_text:    str
    date:        date
    source_id:   str           # e.g. "sg_law_watch", "elitigation"
    item_type:   "news" | "judgment"

EnrichedItem:
    # from raw
    source_url, title, raw_text, date, source_id, item_type

    # from LLM extraction
    topics:      list[TopicExtraction]

    # from FOLIO resolution (applied per-topic)
    # see TopicExtraction below

TopicExtraction:
    headline:        str
    summary:         str
    significance:    "high" | "medium" | "low"
    why_it_matters:  str

    # raw LLM output (free text, pre-resolution)
    raw_areas:       list[str]       # e.g. ["Employment", "Regulatory"]
    raw_entities:    list[str]       # e.g. ["Ministry of Manpower", "Employment Act"]
    raw_concepts:    list[str]       # e.g. ["constructive dismissal", "wrongful termination"]

    # after FOLIO resolution
    folio_areas:     list[FolioRef]
    folio_entities:  list[FolioRef]
    folio_concepts:  list[FolioRef]
    unresolved:      list[str]       # terms FOLIO couldn't match — flag for review

FolioRef:
    iri:             str             # e.g. "https://openlegalstandard.org/ontology/employment-law"
    preferred_label: str             # e.g. "Employment Law"
    branch:          str             # e.g. "areas_of_law"
    confidence:      float           # how good the match was

# judgment-specific extensions
JudgmentMeta:
    citation:        str             # e.g. "[2026] SGHC 34"
    court:           FolioRef        # resolved against FOLIO forum_venues
    judges:          list[str]
    parties:         list[str]
    issues:          list[JudgmentIssue]
    legislation:     list[FolioRef]
    cases_cited:     list[CaseCitation]
    orders:          str

JudgmentIssue:
    question:        str             # "Whether the defendant owed a duty of care"
    holding:         str             # "The court held that no duty arose"
    reasoning:       str             # 2-3 sentence summary of the reasoning
    folio_concepts:  list[FolioRef]  # legal concepts engaged by this issue

CaseCitation:
    citation:        str
    treatment:       "followed" | "distinguished" | "overruled" | "referred"
```

---

## 2. News Pipeline

```
function process_news(raw: RawItem) -> EnrichedItem:

    # ── Step 1: LLM extraction ──────────────────────────────
    #
    # Single API call. The prompt asks the model to split
    # multi-topic articles and extract structured fields.
    # Use tool_use / structured output to force JSON conformance.

    llm_result = call_llm(
        system = NEWS_SYSTEM_PROMPT,
        user   = format_news_user_prompt(raw.source_url, raw.raw_text),
        tools  = [NewsExtractionTool]   # JSON schema enforcing TopicExtraction shape
    )

    topics = parse_tool_result(llm_result)
    # topics is list[TopicExtraction] with raw_areas, raw_entities, raw_concepts filled
    # folio_* fields are empty at this point


    # ── Step 2: FOLIO resolution ─────────────────────────────
    #
    # For each topic, resolve free-text labels to FOLIO IRIs.

    for topic in topics:
        topic = resolve_with_folio(topic)


    # ── Step 3: Deduplication check ──────────────────────────
    #
    # Compare against recent items in the database.
    # Use a combination of URL match (exact dedup) and
    # embedding similarity (fuzzy dedup for same story from
    # different sources).

    for topic in topics:
        if is_duplicate(topic, lookback_days=7):
            topic.is_duplicate = true
            topic.duplicate_of = matched_item_id


    # ── Step 4: Store ────────────────────────────────────────

    enriched = EnrichedItem(raw, topics)
    db.save(enriched)
    return enriched
```

---

## 3. Judgment Pipeline

```
function process_judgment(raw: RawItem) -> EnrichedItem:

    token_count = count_tokens(raw.raw_text)


    # ── Step 1: Triage ───────────────────────────────────────

    if token_count < 5_000:
        path = "short"
    elif token_count < 20_000:
        path = "medium"
    else:
        path = "long"


    # ── Step 2: Structural extraction ────────────────────────
    #
    # Goal: identify the skeleton of the judgment before
    # attempting to summarize. This is a DIFFERENT prompt
    # from the summary prompt — it's asking "what are the
    # parts" not "what does it mean."

    if path == "short":
        # single-pass extraction: structure + summary together
        structure = call_llm(
            system = JUDGMENT_SINGLE_PASS_PROMPT,
            user   = raw.raw_text,
            tools  = [JudgmentExtractionTool]
        )

    elif path == "medium":
        # two-pass: extract structure first, then summarize per issue
        structure = call_llm(
            system = JUDGMENT_STRUCTURE_PROMPT,
            user   = raw.raw_text,
            tools  = [JudgmentStructureTool]
        )
        # structure now contains: citation, court, parties, judges,
        # list of issues (question only), legislation mentioned,
        # cases cited

    elif path == "long":
        # chunked extraction: split into overlapping chunks,
        # extract structure from each, merge
        chunks = split_with_overlap(raw.raw_text, chunk_size=15_000, overlap=1_000)

        partial_structures = []
        for chunk in chunks:
            partial = call_llm(
                system = JUDGMENT_CHUNK_STRUCTURE_PROMPT,
                user   = chunk,
                tools  = [JudgmentChunkTool]
            )
            partial_structures.append(partial)

        structure = merge_structures(partial_structures)
        # merge_structures handles dedup of issues, reconciles
        # conflicting extractions, builds unified case citation list


    # ── Step 3: Issue-level summarization ────────────────────
    #
    # For medium and long judgments, we now summarize each
    # identified issue individually. This is the key insight:
    # we're not asking "summarize this 40,000 word document"
    # — we're asking "for this specific legal issue, what did
    # the court decide and why?"

    if path in ("medium", "long"):
        for issue in structure.issues:

            # extract the relevant portion of the judgment text
            # for this issue (using paragraph references from
            # the structure extraction)
            relevant_text = extract_relevant_paragraphs(raw.raw_text, issue)

            issue_summary = call_llm(
                system = JUDGMENT_ISSUE_SUMMARY_PROMPT,
                user   = format_issue_prompt(issue.question, relevant_text),
                tools  = [IssueSummaryTool]
            )

            issue.holding   = issue_summary.holding
            issue.reasoning = issue_summary.reasoning
            issue.raw_concepts = issue_summary.concepts


    # ── Step 4: Generate the "cookie" layer ──────────────────
    #
    # From the structured data, produce the same TopicExtraction
    # shape that the news pipeline produces. This is what makes
    # both pipelines interchangeable downstream.

    topic = call_llm(
        system = JUDGMENT_COOKIE_PROMPT,
        user   = format_judgment_cookie_prompt(structure),
        tools  = [NewsExtractionTool]   # same schema as news — intentionally
    )
    # This prompt receives the STRUCTURED data (not the raw text)
    # and produces: headline, summary, significance, why_it_matters,
    # raw_areas, raw_entities, raw_concepts


    # ── Step 5: FOLIO resolution ─────────────────────────────
    #
    # Same as news pipeline — resolve all free-text labels
    # to FOLIO IRIs. But for judgments we also resolve:
    # - court name against FOLIO forum_venues
    # - legislation against FOLIO legal_authorities (if available)
    # - each issue's concepts individually

    topic = resolve_with_folio(topic)

    structure.court = resolve_folio_venue(structure.court_name)

    for issue in structure.issues:
        issue.folio_concepts = resolve_folio_concepts(issue.raw_concepts)

    for leg in structure.legislation:
        leg.folio_ref = resolve_folio_authority(leg.name)

    for case in structure.cases_cited:
        # cases won't have FOLIO IRIs, but we can try to
        # match them to items already in our own database
        case.internal_ref = db.find_by_citation(case.citation)


    # ── Step 6: Store ────────────────────────────────────────

    enriched = EnrichedItem(raw, topics=[topic], judgment_meta=structure)
    db.save(enriched)
    return enriched
```

---

## 4. FOLIO Resolution (shared by both pipelines)

```
function resolve_with_folio(topic: TopicExtraction) -> TopicExtraction:

    # ── Areas of law ─────────────────────────────────────────

    for raw_area in topic.raw_areas:
        results = folio_api.search_concepts(
            query  = raw_area,
            branch = "areas_of_law"
        )

        best = pick_best_match(results, raw_area)

        if best and best.score > CONFIDENCE_THRESHOLD:
            topic.folio_areas.append(FolioRef(
                iri             = best.iri,
                preferred_label = best.preferred_label,
                branch          = "areas_of_law",
                confidence      = best.score
            ))
        else:
            topic.unresolved.append(raw_area)


    # ── Entities ─────────────────────────────────────────────
    #
    # Entities are trickier. FOLIO has branches for
    # governmental_bodies, standards_compatibility, etc.
    # We search across multiple branches.

    for raw_entity in topic.raw_entities:
        results = folio_api.search_concepts(
            query = raw_entity
            # no branch filter — search across all branches
        )

        best = pick_best_match(results, raw_entity)

        if best and best.score > CONFIDENCE_THRESHOLD:
            topic.folio_entities.append(FolioRef(
                iri             = best.iri,
                preferred_label = best.preferred_label,
                branch          = best.branch,
                confidence      = best.score
            ))
        else:
            # entity not in FOLIO — store raw string, flag for review
            # this is expected for Singapore-specific bodies like
            # "Building and Construction Authority" or "PDPC"
            topic.folio_entities.append(FolioRef(
                iri             = None,
                preferred_label = raw_entity,
                branch          = "unresolved",
                confidence      = 0.0
            ))
            topic.unresolved.append(raw_entity)


    # ── Legal concepts ───────────────────────────────────────
    #
    # These are doctrines, tests, principles.
    # e.g. "duty of care", "res judicata", "extended doctrine
    # of abuse of process"

    for raw_concept in topic.raw_concepts:
        results = folio_api.search_concepts(query=raw_concept)
        # might also try search_definitions for longer concept phrases

        best = pick_best_match(results, raw_concept)

        if best and best.score > CONFIDENCE_THRESHOLD:
            topic.folio_concepts.append(FolioRef(
                iri             = best.iri,
                preferred_label = best.preferred_label,
                branch          = best.branch,
                confidence      = best.score
            ))
        else:
            topic.unresolved.append(raw_concept)


    return topic


function pick_best_match(results, query) -> FolioResult | None:
    #
    # Ranking strategy:
    #
    # 1. Exact label match (case-insensitive)         → score 1.0
    # 2. Preferred label contains query or vice versa  → score 0.8
    # 3. FOLIO's search relevance score                → as returned
    # 4. No results or all below threshold             → None
    #
    # CONFIDENCE_THRESHOLD = 0.6 (tunable)
    #
    # In ambiguous cases (multiple close matches), prefer:
    # - more specific concepts over general ones
    #   (i.e. deeper in the taxonomy tree)
    # - concepts whose parent chain aligns with other
    #   resolved concepts in the same topic

    if not results:
        return None

    for r in results:
        if r.preferred_label.lower() == query.lower():
            r.score = 1.0
            return r

    # fall back to best FOLIO search score
    best = max(results, key=lambda r: r.relevance_score)
    best.score = normalize_score(best.relevance_score)
    return best if best.score > CONFIDENCE_THRESHOLD else None
```

---

## 5. The Daily Orchestrator

```
function run_daily():

    # ── Collect ──────────────────────────────────────────────

    news_items     = collect_all_news_sources()       # list[RawItem]
    judgment_items = collect_elitigation_firehose()    # list[RawItem]

    log(f"Collected {len(news_items)} news, {len(judgment_items)} judgments")


    # ── Process ──────────────────────────────────────────────

    enriched = []

    for item in news_items:
        result = process_news(item)
        enriched.append(result)

    for item in judgment_items:
        result = process_judgment(item)
        enriched.append(result)


    # ── Compute daily stats ──────────────────────────────────

    stats = DailyStats(
        date             = today(),
        total_items      = len(enriched),
        news_count       = count(enriched, type="news"),
        judgment_count   = count(enriched, type="judgment"),
        high_significance = filter(enriched, significance="high"),
        medium_significance = filter(enriched, significance="medium"),
        areas_breakdown  = count_by_folio_area(enriched),
        courts_breakdown = count_by_court(enriched),
        busiest_area     = top(areas_breakdown),
        duplicates_found = count(enriched, is_duplicate=true),
        unresolved_terms = collect_all_unresolved(enriched)
    )

    db.save_daily_stats(stats)


    # ── Distribute ───────────────────────────────────────────

    # 1. The daily cookie (data-focused digest for everyone)
    cookie = generate_daily_cookie(stats, enriched)
    publish_to_blog(cookie)

    # 2. Topic notifications (personalised)
    subscriptions = db.get_all_subscriptions()
    for sub in subscriptions:
        matching = filter_by_folio_areas(
            enriched,
            sub.folio_area_iris,
            min_significance="medium"
        )
        if matching:
            send_notification(sub.user, matching, channel=sub.preferred_channel)

    # 3. Weekly rollup (if it's Sunday)
    if today().weekday() == 6:
        week_items = db.get_items(last_7_days)
        week_stats = compute_weekly_stats(week_items)
        rollup = generate_weekly_rollup(week_stats, week_items)
        publish_to_blog(rollup)
        send_to_email_subscribers(rollup)


    # ── Housekeeping ─────────────────────────────────────────

    # Log unresolved FOLIO terms for periodic review.
    # These are candidates for:
    # - Singapore-specific FOLIO extensions
    # - prompt improvements (if the LLM is extracting junk)
    # - new taxonomy entries

    if stats.unresolved_terms:
        log_for_review(stats.unresolved_terms)


    log(f"Daily run complete. {stats.total_items} items processed.")
```

---

## 6. The Notification Filter (FOLIO-powered)

```
function filter_by_folio_areas(items, subscribed_iris, min_significance):
    #
    # This is where FOLIO's hierarchy pays off.
    #
    # If a user subscribes to "Employment Law" (a parent concept),
    # they should also receive items tagged with child concepts
    # like "Workplace Safety" or "Wrongful Termination."
    #
    # We expand the user's subscribed IRIs to include all
    # descendants in the FOLIO taxonomy.

    expanded_iris = set()
    for iri in subscribed_iris:
        expanded_iris.add(iri)
        children = folio_api.get_children(iri, recursive=true)
        for child in children:
            expanded_iris.add(child.iri)

    matching = []
    for item in items:
        if significance_rank(item.significance) < significance_rank(min_significance):
            continue

        for topic in item.topics:
            topic_iris = {ref.iri for ref in topic.folio_areas}
            if topic_iris & expanded_iris:      # set intersection
                matching.append(item)
                break

    return matching
```

---

## Notes

**On the FOLIO confidence threshold:** Start at 0.6 and tune based on
what lands in `unresolved`. If too many legitimate terms are unresolved,
lower it. If junk matches are getting through, raise it. Log everything
so you can audit.

**On Singapore-specific gaps:** Expect `unresolved` to be noisy at first
with Singapore entities (PDPC, BCA, HDB, CPF Board, eLitigation-specific
court divisions). Build a local mapping table for these, and consider
contributing them back to FOLIO as a Singapore extension module.

**On API costs:** The news pipeline is 1 LLM call per article. The
judgment pipeline is 1-N calls depending on length and issue count.
FOLIO API calls are free and fast. The main cost driver is long
judgments — the chunked extraction path for a 50,000-word Court of
Appeal decision might be 5-8 LLM calls. Budget accordingly.

**On the "cookie" shape:** Both pipelines produce TopicExtraction
objects with the same fields. The judgment pipeline ALSO produces
JudgmentMeta with richer structured data. The daily cookie generator
only needs TopicExtraction. The MCP endpoint and search features
can use JudgmentMeta for deeper queries. This separation keeps the
common path simple while preserving depth for power users.
