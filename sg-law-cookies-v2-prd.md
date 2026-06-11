# SG Law Cookies v2 — Product Requirements Document

## 1. Vision

SG Law Cookies processes the entire legal output of Singapore every day
and helps people find what matters to them.

The current product is a blog that summarises news articles from
Singapore Law Watch. The new product is a **legal signal platform**:
an ingestion and enrichment pipeline that produces structured,
ontology-grounded data, distributed through multiple channels and
visualised on a homepage that makes the daily state of Singapore law
legible at a glance.

The blog becomes one output among many. The pipeline and the database
are the product.

v2 is also the flagship showcase for **data.zeeker.sg**: all
ingestion reads from Zeeker's curated Singapore legal databases,
and the daily cookie is framed as **"Zeeker's updates today."**
The product demonstrates what can be built on Zeeker's open data.

---

## 2. Core Concepts

### 2.1 What is a Cookie?

A cookie is the **smallest unit of legal change that a practitioner
might need to act on, be aware of, or track.**

A cookie is NOT a summary. A summary tries to compress a source
faithfully. A cookie extracts the **signal** from a source and
discards the rest. A 40,000-word Court of Appeal judgment might
produce one cookie: "Court of Appeal holds that arbitral findings
can ground abuse-of-process arguments in subsequent litigation."

**Cookies are signals.** They come in three tiers:

| Tier | Description | Example |
|------|-------------|---------|
| Act on | New obligation, deadline, changed threshold. Requires action. | EP salary floor rising to SGD 6,000 from Jan 2027. |
| Be aware of | Shifts how a doctrine is applied. Update your mental model. | CA distinguishes prior authority on abuse of process. |
| Track | Individually low-signal, meaningful in aggregate. Patterns emerge over time. | Routine sentencing decision in drug trafficking. |

**Key properties of a cookie:**

- **Format-consistent.** Every cookie, whether from a 200-word
  news brief or a 50,000-word judgment, has the same shape:
  headline, 2–3 sentence summary, "why it matters" sentence.
- **Source-decoupled.** The mapping between sources and cookies is
  many-to-many. One source can produce multiple cookies. Multiple
  sources can corroborate one cookie.
- **Proposition-driven.** The number of cookies from a source is
  determined by the number of distinct legal propositions it
  contains, not by its length.
- **FOLIO-grounded.** Every cookie is tagged with standardised
  ontology concepts, not ad-hoc labels.

### 2.2 What is a Source?

A source is a document ingested by the pipeline. All sources
arrive through one channel: **data.zeeker.sg**. Zeeker's curated
databases replace per-source scrapers; ingestion is an incremental
query ("new rows since the last run"). Sources come in two
pipeline types:

| Type | Zeeker databases | Characteristics |
|------|------------------|-----------------|
| News article | `sglawwatch` (headlines, commentaries), `sg-gov-newsrooms` (ministry and agency press releases) | Short-to-medium length (500–3,000 words). Usually covers 1–4 topics. Key facts often in opening paragraphs. |
| Judgment | `zeeker-judgements` (Court of Appeal, High Court, subordinate courts), `pdpc` (enforcement decisions) | Variable length (1,000–80,000+ words). Deep internal structure (facts, issues, holdings, reasoning, obiter). Signal may be buried deep in the text. Zeeker provides `content_text`, `court_summary`, and `subject_tags` where available — used to seed extraction. |

These are the only two pipeline types. All sources are classified
as one or the other. When Zeeker adds a new database in future
(e.g. parliamentary hansards, gazette notices), it routes to
whichever pipeline best fits its structure.

**What "today" means.** A day's cookies are the items that newly
appeared in Zeeker since the previous run — the daily cookie is
Zeeker's updates for the day. A judgment's `decision_date` may be
earlier than the day it surfaces in Zeeker; both dates are stored,
and the decision date is displayed on the cookie.

### 2.3 Significance

Significance is a **visual and sorting primitive**, not an editorial
gatekeeping mechanism. Every cookie is published. Nothing is
suppressed. Significance determines prominence, not inclusion.

| Level | Definition | Pipeline behaviour |
|-------|------------|--------------------|
| High | Creates new legal obligations, changes existing law, introduces a new statutory framework. | Displayed prominently in constellation. Named in daily digest. Triggers push notifications for subscribers. |
| Medium | Updates or clarifies existing rules, adjusts thresholds, extends existing schemes. | Visible in constellation with labels on hover. Included in topic-filtered notifications. |
| Low | Commentary, market data, general interest, routine proceedings. | Present as dots in constellation. Searchable in archives and MCP. Not in digest or notifications unless user explicitly opts in. |

**Significance is the pipeline's first-pass estimate.** The
personalisation layer overrides it: a low-significance PDPC
enforcement action becomes high-significance to a data protection
lawyer through their subscription. Over time, usage data (clicks,
MCP queries, references by later cookies) provides a feedback
signal for improving significance ratings.

### 2.4 Shelf Life

Shelf life is NOT computed up front. It emerges from usage data
over time. For now, recency is the default decay function.

Two dimensions to consider for future development:

- **Urgency:** Does this cookie have a time-bound action? Filing
  deadlines, commencement dates, consultation periods. These are
  urgent and then expire.
- **Durability:** Will someone searching in 6 months find this
  useful? Landmark cases are permanently durable. Routine
  procedural decisions are ephemeral.

### 2.5 Attribution and Linking

Two rules apply across every channel (site, email, MCP):

1. **Attribute Zeeker.** Cookies are derived from data.zeeker.sg.
   Every surface that displays cookies carries a visible
   "Data: data.zeeker.sg" attribution. Zeeker is a **catalogue**:
   it indexes and points at source documents rather than
   reproducing them, and the licence labels in its metadata
   describe the underlying sources' terms. The cookies app adopts
   the same posture — source text is used internally for
   extraction only and never republished; cookies are original
   derived text with outbound links. The per-source licence is
   still captured at ingest so every channel knows the terms of
   the document it points at.
2. **Link to the original source.** Any further reading — the full
   judgment, the underlying article, the press release — points at
   the original source URL (Judiciary site, news outlet, ministry
   newsroom), never at Zeeker. Zeeker is the data layer, not the
   reading destination.

---

## 3. Architecture

### 3.1 System Overview

```
  ┌─────────────────────────────────────────────┐
  │               data.zeeker.sg                 │
  │  sglawwatch · sg-gov-newsrooms ·             │
  │  zeeker-judgements · pdpc                    │
  └────────┬───────────────────────┬────────────┘
           │                       │
     ┌─────▼──────┐        ┌──────▼───────┐
     │   News     │        │  Judgment    │
     │  Pipeline  │        │  Pipeline    │
     └─────┬──────┘        └──────┬───────┘
           │                      │
           │   ┌──────────────┐   │
           └──►│   Shared     │◄──┘
               │  Database    │
               │  (SQLite)    │
               └──────┬──────┘
                      │
        ┌─────────────┼─────────────────┐
        │             │                 │
  ┌─────▼────┐  ┌────▼─────┐   ┌──────▼──────┐
  │  Blog /  │  │  Email   │   │  MCP        │
  │  Constel-│  │  Digest  │   │  Endpoint   │
  │  lation  │  │          │   │             │
  └──────────┘  └──────────┘   └─────────────┘
        │             │                 │
        ▼             ▼                 ▼
    Homepage      Subscribers       Developers /
    visitors      (topic-based)     Power users
```

### 3.2 Four Layers

**Layer 1 — Ingestion.** A single Zeeker client that queries each
Zeeker database for rows added since the last run (watermark on
Zeeker's `created_at`) and normalises them into a common `RawItem`
format: title, original source URL, raw text, dates, source
identifier, item type. Zeeker's rate limits (60/min, 5,000/day per
IP) comfortably cover daily volumes, and watermarks make collection
idempotent across re-runs.

**Catalogue discovery.** Zeeker is actively expanding. At the start
of each run, the client enumerates Zeeker's databases and tables
(both the MCP and the underlying Datasette JSON API expose this)
and diffs the result against a local source registry. Known sources
carry a pipeline routing (news or judgment), a licence, and a
watermark. A newly appeared database or table is logged and
alerted, then activated by adding a one-line registry entry that
assigns its routing — new sources are surfaced automatically but
routed deliberately, since a wrong pipeline assignment produces
garbage cookies. Licence metadata is re-read on every run, as it
can change.

**Layer 2 — Enrichment.** Where the LLM extracts signals and FOLIO
grounds them. This is the core of the system. Two sub-pipelines
(news and judgment) converge on the same output types.

**Layer 3 — Storage.** SQLite database holding every source and
cookie with enriched metadata. Single source of truth. Optionally
with sqlite-vec for embedding-based search and deduplication.

**Layer 4 — Distribution.** Multiple independent outputs reading
from the same enriched store. Adding a new channel should be
trivial because the hard work is done upstream.

### 3.3 Technology Choices

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Data source | data.zeeker.sg (Datasette JSON API / MCP) | A catalogue that indexes and points at sources, not a republisher. Self-describing, so new databases are auto-discoverable. Underlying source licences vary per database — captured at ingest. One client replaces all per-source scrapers. This project is the showcase consumer. |
| Language | Python, end-to-end | Single codebase for pipeline, site generation, and distribution. |
| Package management | uv | Fast, modern, already in use in current repo. |
| LLM | Anthropic API with tool use (primary); Ollama structured outputs (local alternative) | Both enforce the same JSON schema, so backends are interchangeable. Ollama (e.g. gemma4:26b) enables free local/dev runs — disable thinking traces, which are prohibitively slow at local token speeds. |
| Orchestration | Plain Python, no framework (no Rivet) | Full control, easy to version and test, no stale dependency risk. |
| Database | SQLite + sqlite-vec | No database server. Full query capability. Embedding search for dedup. |
| Ontology | FOLIO (openlegalstandard.org) | 18,000+ standardised concepts. CC-BY licensed. Python library and public API. MCP server available. |
| Static site | Jinja2 templates or lightweight framework | Replaces Hugo. Keeps everything in Python. |
| Orchestration host | Small VPS with cron | Runs the daily pipeline, holds the SQLite database, serves the MCP endpoint, pushes static output to the static host. |
| Visualisation | D3.js with d3-force | Force-directed constellation layout. Static JSON data files generated by pipeline. |
| Hosting | Cloudflare Pages at **cookies.zeeker.sg** + VPS (MCP endpoint) | Static site on a zeeker.sg subdomain — the showcase relationship in the hostname. Live queries served from the VPS. |

---

## 4. Enrichment Pipeline

### 4.1 News Pipeline

**Input:** A `RawItem` with item_type "news".
**Output:** One or more `Cookie` objects stored in the database.
**Cost:** 1 LLM call per article + FOLIO API calls for resolution.

```
Ingest → LLM extraction (single pass) → FOLIO resolution → Dedup check → Store
```

**Step 1 — LLM Extraction.** A single API call using tool use to
enforce the output schema. The prompt instructs the model to:

- Split multi-topic articles into separate cookie objects
- For each cookie: extract headline, summary, why_it_matters
- Extract free-text areas of law, entities, and legal concepts
- Assign significance rating using the defined rubric
- Not copy verbatim from the source
- Not hallucinate facts

The model returns structured JSON conforming to the tool schema.
No free-text JSON parsing needed.

**Step 2 — FOLIO Resolution.** For each free-text label extracted
by the LLM, query the FOLIO API to resolve to a standardised IRI.
Three resolution passes:

- `raw_areas` → search against FOLIO `areas_of_law` branch
- `raw_entities` → search across all FOLIO branches
- `raw_concepts` → search for legal doctrines and principles

Terms that don't resolve above a confidence threshold (starting at
0.6, tunable) are stored in an `unresolved` list for review.

**Step 3 — Deduplication.** Compare incoming cookies against recent
items in the database. URL match for exact dedup. Embedding
similarity for fuzzy dedup (same story from different sources).
Duplicates are flagged, not deleted — they're useful as
corroboration signals.

**Step 4 — Store.** Save enriched cookies and their source linkage
to the database.

### 4.2 Judgment Pipeline

**Input:** A `RawItem` with item_type "judgment".
**Output:** One or more `Cookie` objects plus a `JudgmentMeta`
record with deeper structured data.
**Cost:** 1–8 LLM calls depending on judgment length and issue count.

```
Ingest → Triage → Structural extraction → Issue-level summarisation
       → Cookie generation → FOLIO resolution → Store
```

**Step 1 — Triage.** Route by token count. No LLM call needed.

| Path | Token count | Processing strategy |
|------|-------------|---------------------|
| Short | < 5,000 | Single-pass extraction (structure + summary together) |
| Medium | 5,000–20,000 | Two-pass (extract structure, then summarise per issue) |
| Long | > 20,000 | Chunked extraction with overlap, merge, then summarise per issue |

**Step 2 — Structural Extraction.** Identify the skeleton of the
judgment before attempting to summarise. This is a classification
task, not a summarisation task. Extracts:

- Case citation, court, judge(s), parties, date
- Factual background (compressed)
- Legal issues before the court (as questions)
- Paragraph references for where each issue is discussed
- Legislation considered
- Cases cited and their treatment (followed / distinguished / overruled)
- Orders made

For long judgments, this runs per-chunk with overlapping windows,
then a merge step reconciles and deduplicates the partial extractions.

**Step 3 — Issue-Level Summarisation.** For each legal issue
identified in Step 2, extract the relevant portion of the judgment
text and produce a focused summary: what was the question, what did
the court decide, and what was the core reasoning. This is the
richest layer of data and powers deep queries via MCP.

**Step 4 — Cookie Generation.** A separate LLM call that receives
the *structured data from Steps 2–3* (not the raw judgment text)
and produces cookies in the same format as the news pipeline. The
number of cookies is determined by the number of issues with
independent signal value, not by the length of the judgment.

**Step 5 — FOLIO Resolution.** Same as the news pipeline, plus:

- Court name resolved against FOLIO `forum_venues` branch
- Legislation resolved against FOLIO `legal_authorities` (if available)
- Each issue's concepts resolved individually
- Cases cited matched against the internal database for cross-linking

**Step 6 — Store.** Save cookies, judgment metadata, and source
linkage.

### 4.3 FOLIO Resolution (Shared)

FOLIO resolution is a **deterministic post-processing step**, not
part of the LLM prompt. The LLM extracts free-text labels. FOLIO
resolves them to standardised IRIs. This separation means:

- FOLIO matching can be improved without re-running LLM calls
- The `unresolved` list provides a clear feedback signal
- Singapore-specific gaps in FOLIO are surfaced automatically

**Matching strategy:**

| Match type | Score |
|------------|-------|
| Exact label match (case-insensitive) | 1.0 |
| Preferred label contains query or vice versa | 0.8 |
| FOLIO API relevance score (normalised) | As returned |
| No results or all below threshold | Unresolved |

**Confidence threshold:** 0.6 (tunable). Start here, adjust based
on the volume and quality of unresolved terms.

**Hierarchy expansion.** When filtering for subscriptions, a user
subscribed to a parent FOLIO concept (e.g. "Employment Law")
automatically receives items tagged with any child concept (e.g.
"Workplace Safety", "Wrongful Termination"). The FOLIO API's
`get_children` endpoint supports recursive traversal.

**Singapore-specific gaps.** FOLIO is primarily developed around
common law systems with a US/international focus. Expect the
`unresolved` list to be noisy at first with Singapore-specific
entities (PDPC, BCA, HDB, CPF Board, specific statutory boards
and tribunals). Build a local mapping table for these. Consider
contributing them back to FOLIO as a Singapore extension module.

---

## 5. Data Model

### 5.1 Core Types

```
Source:
    id:           str (UUID)
    source_url:   str        # ORIGINAL document URL (Judiciary, outlet,
                             # newsroom) — used for all outbound links
    zeeker_url:   str        # Zeeker row URL, for attribution/provenance
    title:        str
    raw_text:     str
    date:         date       # document date (e.g. judgment decision_date)
    source_id:    str        # Zeeker database, e.g. "sglawwatch",
                             # "zeeker-judgements", "pdpc"
    item_type:    "news" | "judgment"
    license:      str        # underlying source's terms, from Zeeker
                             # catalogue metadata at ingest
    token_count:  int
    ingested_at:  datetime   # when WE saw it in Zeeker — defines "today"

Cookie:
    id:           str (UUID)
    source_ids:   list[str]  # links to one or more Sources
    headline:     str
    summary:      str        # 2–3 sentences
    why_it_matters: str      # 1 sentence, practical implication
    significance: "high" | "medium" | "low"
    folio_areas:  list[FolioRef]
    folio_entities: list[FolioRef]
    folio_concepts: list[FolioRef]
    unresolved:   list[str]
    is_duplicate: bool
    duplicate_of: str | null
    created_at:   datetime

FolioRef:
    iri:             str
    preferred_label: str
    branch:          str
    confidence:      float

JudgmentMeta:                 # only for judgment sources
    source_id:    str         # links to Source
    citation:     str         # e.g. "[2026] SGHC 34"
    court:        FolioRef
    judges:       list[str]
    parties:      list[str]
    issues:       list[JudgmentIssue]
    legislation:  list[FolioRef]
    cases_cited:  list[CaseCitation]
    orders:       str

JudgmentIssue:
    question:        str
    holding:         str
    reasoning:       str      # 2–3 sentence summary
    folio_concepts:  list[FolioRef]

CaseCitation:
    citation:     str
    treatment:    "followed" | "distinguished" | "overruled" | "referred"
    internal_ref: str | null  # link to our own DB if we have this case

DailyStats:
    date:              date
    total_cookies:     int
    news_count:        int
    judgment_count:    int
    high_significance: list[str]   # cookie IDs
    medium_significance: list[str]
    areas_breakdown:   dict[str, int]
    courts_breakdown:  dict[str, int]
    busiest_area:      str
    unresolved_terms:  list[str]

# Subscriptions and user data are OUT OF SCOPE for the cookies
# app. They live in a separate app (not yet in production) that
# reads enriched cookies from this database. Sketch of the shape
# that app is expected to hold, kept here as an interface contract:

Subscription:                  # owned by the external subscription app
    user_id:          str
    folio_area_iris:  list[str]
    preferred_channel: "email" | "telegram" | "mcp"
    min_significance: "high" | "medium" | "low"
```

---

## 6. Distribution Channels

### 6.1 Daily Cookie (The Blog / Homepage)

**Format:** Data-focused digest. Not "here are today's important
stories" (every legal news service does that). Instead, a
**dashboard for the firehose** — the shape and texture of the day.

**Tone:** Light, informative, almost playful. The kind of thing you
glance at over coffee.

**Look (settled, June 2026).** The site is a **traditional Singapore
bakery shopfront** built on data.zeeker.sg's design tokens — visual
kinship with Zeeker is a requirement (this is its showcase), and the
reference is Love Confectionery in Ang Mo Kio. Approved mockup:
`mockups/f-bakery-lovecon.html`. The component vocabulary:

- **Signboard** header: cream panel, brush-script wordmark, red
  法律饼家, unit number; yellow ceiling pipe above.
- **Yellow price cards** for counts and per-cookie labels; price
  encodes significance (pineapple tart = act on, $0.60 fresh,
  $0.30 tracking).
- **Display case** as the daily scroll: walnut frame, glass
  reflections, aluminium trays, one bake per cookie. Round = news,
  hexagonal shortbread = judgments, **pineapple tart = high
  significance** (overrides shape). Chocolate chips = FOLIO
  concepts. Steam = still warm.
- **Baker's docket** (perforated receipt) for daily stats,
  including the day's "new ingredient" (an unresolved FOLIO term).
- **Pan ticker**: aluminium tray marquee of areas with counts.
- **Specials board** (dark ink panel) for high-significance
  cookies; **dot-leader menu** grouped by area for mediums;
  **cooling rack** strip for lows; mosaic kerb + brick five-foot
  way at the page foot.
- Light canvas throughout; teal/ochre/terracotta from Zeeker's
  palette; serif body + JetBrains Mono labels. The constellation
  (Phase 5+) lives on its own page, linked from the homepage.

**Content:**
- Daily stats: total cookies, news vs. judgment split, area breakdown
- One-liner on each high-significance cookie
- Links to explore medium-significance cookies by area
- The constellation visualisation as the hero element

**Cadence:** Daily, published automatically after the pipeline run.

**Attribution:** The page is titled and framed as Zeeker's updates
for the day, carries the data.zeeker.sg attribution, and every
"read the source" link points at the original document (§2.5).

### 6.2 Email Digest

**Ownership:** Subscriptions and delivery live in a separate app
(not yet in production). The cookies app's responsibility ends at
exposing enriched, FOLIO-tagged cookies for that app to filter and
send.

**Format:** Personalised by subscription topics. Only delivers
cookies matching the subscriber's FOLIO area subscriptions at their
chosen minimum significance threshold.

**Provider:** Buttondown, Resend, or similar.

**Cadence:** Daily or weekly (subscriber's choice).

**Conversion hook:** "You're seeing 3 Employment cookies today
because you subscribed to Employment Law. You missed 2 Banking
cookies — add Banking & Finance to your profile?"

### 6.3 MCP Endpoint

**Format:** Structured query interface for developers and power users.

**Capabilities:**
- Query by FOLIO concept IRI, date range, significance level
- Full-text search across cookie summaries
- Judgment-specific queries: by court, citation, legislation, cases cited
- Return cookies with full metadata including FOLIO references
- Cross-reference queries: "what cases cite this case?"

**Example queries:**
- "What judgments in the last 30 days dealt with contractual interpretation?"
- "Were there any PDPC enforcement actions this week?"
- "Show me all high-significance employment cookies since January."

### 6.4 Additional Channels (Future)

| Channel | Fit for Singapore audience | Notes |
|---------|---------------------------|-------|
| Telegram bot | High — widely used among SG professionals | Topic-subscribed push notifications |
| LinkedIn posts | High — natural for legal professionals | Auto-post daily digest teaser with link |
| WhatsApp broadcast | Medium — personal messaging, high open rates | Requires business API |
| RSS feed | Low volume but valued by power users | Already supported, retain it |

---

## 7. Constellation Visualisation

### 7.1 Design Philosophy

The constellation is the **visual front door** to the product. It
communicates three things instantly:

1. **Scale** — we process a lot, every day (builds trust)
2. **Structure** — the legal landscape has a shape today (invites
   exploration)
3. **Personalisation** — your areas glow differently (drives
   subscription)

### 7.2 Three Zoom Levels

**Level 1 — Daily Skyline (default/homepage view)**

The landing view. Centre node is today's date. Around it orbit
8–12 cluster nodes, one per active FOLIO area of law. Each cluster
is sized by cookie count. Clusters containing high-significance
cookies have a glow or ring effect.

Node count at this level: 10–15. Clean, parseable, calm.

Daily digest stats text sits alongside the constellation.

**Level 2 — Cluster View (click into an area)**

Tapping a cluster expands it. Individual cookies within that area
become visible, arranged around the area node.

- High-significance cookies: large nodes, labels visible by default
- Medium-significance cookies: medium nodes, labels on hover
- Low-significance cookies: small dots, present but not demanding

FOLIO concept nodes appear between cookies, creating edges. Cookies
sharing concepts cluster together naturally. Outliers drift to
the periphery.

**Level 3 — Cookie Detail (click into a cookie)**

A detail panel with the full cookie: headline, summary, why it
matters, source link, FOLIO concept tags. For judgments, a link to
the deeper issue-level analysis.

Subscription prompt: "Want more like this? Subscribe to [area] cookies."

### 7.3 Force-Directed Layout

The spatial arrangement uses a force-directed simulation:

- **Attraction:** Cookies sharing FOLIO concepts attract each other
  (shared concepts create edges)
- **Repulsion:** Cookies repel other cookies slightly (prevent
  overlap)
- **Concept centroids:** Concept nodes sit at the centroid of their
  connected cookies
- **Mass:** High-significance cookies are heavier and pull the
  cluster toward them (important stuff gravitates to centre)
- **Shape encoding:** Circles for news cookies, hexagons for
  judgment cookies

Variable daily volumes handled gracefully: sparse days look airy,
busy days look dense. The physics keeps everything legible.

### 7.4 Time Navigation

A date scrubber below the constellation allows stepping through
previous days. The constellation animates between states. Persistent
concept nodes show continuity. This allows users to **watch
Singapore law move over time**.

Weekly view: days as columns, cookies flowing left to right,
connecting lines for recurring concepts across days. Low-significance
"tracking" cookies become visible as patterns when the same concept
lights up repeatedly.

### 7.5 Personalisation Overlay

For subscribed users, their FOLIO concept subscriptions are
highlighted in a distinct colour. Matching cookies are visually
pulled toward the viewer (brighter, larger, or positioned more
centrally). The same data, but each person's constellation looks
different.

For non-subscribers, a prompt: "What areas do you follow?" with
tappable FOLIO areas. Selecting them immediately recolours the
constellation as a preview of the personalised experience.

### 7.6 Data Format

The pipeline generates a static JSON file per day:

```json
{
  "date": "2026-04-02",
  "stats": {
    "total_cookies": 87,
    "news_count": 15,
    "judgment_count": 72,
    "high_count": 4,
    "medium_count": 23,
    "low_count": 60
  },
  "clusters": [
    {
      "area_iri": "https://openlegalstandard.org/...",
      "area_label": "Employment Law",
      "cookie_count": 23,
      "has_high_significance": true,
      "cookies": [
        {
          "id": "...",
          "headline": "...",
          "significance": "high",
          "source_type": "news",
          "concepts": [
            { "iri": "...", "label": "Work Passes" }
          ]
        }
      ]
    }
  ],
  "edges": [
    { "source_cookie": "...", "target_concept": "...", "weight": 1.0 }
  ]
}
```

No backend API needed. Static files loaded client-side.

### 7.7 Mobile Degradation

On mobile, Level 1 (10–15 nodes) renders as the constellation.
Level 2 degrades to a filtered, sorted list of cookies when a
cluster is tapped. Spatial layout adds value on desktop; a
well-sorted list is more practical on small screens.

### 7.8 Homepage Conversion Funnel

```
Visitor lands
  → sees firehose counter ("87 cookies today") — communicates scale
  → sees constellation skyline — invites exploration
  → taps into a cluster — finds real content
  → reads a few cookies — sees value
  → sees personalisation prompt — subscribes
```

---

## 8. Daily Orchestrator

The daily run is a single Python entry point invoked by cron on
the VPS. Collection is incremental — step 1 fetches only Zeeker
rows newer than the stored watermark — so re-runs are idempotent.

```
1. Collect     — query Zeeker databases for new rows since watermark
2. Process     — route each source through news or judgment pipeline
3. Stats       — compute daily statistics and breakdowns
4. Distribute  — generate blog, send notifications, update JSON files
5. Housekeep   — log unresolved FOLIO terms for review
```

### 8.1 Cost Estimation

| Component | Per-item cost | Daily volume (est.) | Daily cost (est.) |
|-----------|--------------|--------------------|--------------------|
| News pipeline (1 LLM call) | ~$0.01–0.03 | 10–20 articles | $0.10–0.60 |
| Judgment pipeline — short (1 call) | ~$0.01–0.05 | ~15 judgments | $0.15–0.75 |
| Judgment pipeline — medium (2–3 calls) | ~$0.05–0.15 | ~10 judgments | $0.50–1.50 |
| Judgment pipeline — long (5–8 calls) | ~$0.20–0.80 | ~5 judgments | $1.00–4.00 |
| Zeeker API calls | Free (rate-limited) | ~50–200 calls | $0.00 |
| FOLIO API calls | Free | ~500–1000 calls | $0.00 |
| **Total estimated daily** | | | **$1.75–6.85** |

These are rough estimates. Actual costs depend on model choice and
prompt efficiency. Monthly cost estimated at $50–200, manageable
for a side project or small product.

### 8.2 Reliability and Failure Handling

**Daily runs are expected to be flaky.** Zeeker outages, LLM API
errors, rate limits, and malformed judgments are normal operating
conditions, not exceptions. The orchestrator is designed so that a
partial failure never loses data and never requires manual repair:

- **Per-item checkpointing.** Each source is processed and
  committed independently. One bad item never aborts the run.
- **Retry with backoff** for transient Zeeker and LLM errors.
  Items that still fail land in a dead-letter table and are
  retried automatically on the next run.
- **Watermarks advance only on successful store.** A failed item
  is never silently skipped past.
- **Idempotent distribution.** The publish step is safe to re-run
  without duplicate posts or notifications.
- **Run report and alerting.** Every run logs a summary; alerts
  fire on failed runs, zero-collection days, and dead-letter
  growth.

---

## 9. Build Order

Each phase produces something independently useful. Each phase is a
natural stopping point with a working product.

### Phase 1 — Foundation
**Goal:** Data model, storage, one news collector, enrichment pipeline.
- Define SQLite schema matching the data model above
- Build the Zeeker client with watermark-based incremental fetch
  (start with the `sglawwatch` headlines table)
- Build news pipeline: LLM extraction with tool use + FOLIO resolution
- Store enriched cookies in database
- **Deliverable:** A database filling up with structured, FOLIO-grounded cookies daily.

### Phase 2 — Blog Output
**Goal:** Replace Hugo with a Python-generated static site that
looks like a designed product from day one.
- Extract the design system from the approved mockup
  (`mockups/f-bakery-lovecon.html`) into tokens + component CSS
- Jinja2 templates for the daily cookie page, built on those tokens
- Data-focused daily digest format (stats + high-significance one-liners)
- Static site generation from database
- Deploy to Cloudflare Pages / Netlify
- Clean break from the Hugo blog: v1 is retired and old URLs are
  not preserved. Retain an RSS feed at the new site.
- **Deliverable:** Replacement for the current blog, data-driven,
  framed as Zeeker's daily updates.

### Phase 3 — Judgment Pipeline
**Goal:** Process the judgment firehose from Zeeker.
- Extend the Zeeker client to `zeeker-judgements` and `pdpc`
- Build triage step (short / medium / long routing)
- Build staged extraction: structure → issues → cookie generation
- FOLIO resolution for judgment-specific fields (courts, legislation, citations)
- Deduplication across news and judgment sources
- **Deliverable:** Full firehose processing. Database now contains comprehensive daily legal output of Singapore.

### Phase 4 — Pipeline Hardening
**Goal:** Make the daily run survive flaky reality (see §8.2).
Runs are expected to fail partially; the pipeline must degrade
gracefully and recover without manual intervention.
- Per-item checkpointing — a failed item never aborts the run
- Retry with backoff; dead-letter table with automatic
  reprocessing on the next run
- Watermark advances only on successful store
- Idempotent publish step (safe to re-run, no double-sends)
- Run summary logging and failure alerting (failed run,
  zero-collection day, dead-letter growth)
- **Deliverable:** A pipeline you can ignore for a week without
  losing data.

### Phase 5 — Constellation Visualisation
**Goal:** Level 1 skyline on the homepage.
- Generate daily constellation JSON from pipeline stats
- Build D3 force-directed visualisation (Level 1: cluster nodes)
- Integrate into static site homepage
- Firehose counter component
- Visual polish pass: constellation uses the Phase 2 design
  system; motion and easing tuned so the skyline feels calm, not
  busy; checked on desktop and mobile
- **Deliverable:** Visual homepage that communicates scale and invites exploration.

### Phase 6 — Constellation Depth
**Goal:** Levels 2 and 3 of the constellation.
- Click-to-expand into cluster view (individual cookies, concept edges)
- Cookie detail panel
- Date scrubber for time navigation
- Mobile degradation (list view for Level 2)
- **Deliverable:** Full interactive constellation experience.

### Phase 7 — Email Distribution (external app integration)
**Goal:** Personalised topic-subscribed email digests, delivered by
the separate subscription app once it reaches production.
- Cookies app: expose a stable read interface (DB views or API)
  for the subscription app to consume
- Subscription app (out of scope here): subscription management,
  FOLIO hierarchy expansion, email rendering and delivery
- Interim option: a plain single-list daily newsletter sent
  directly from the pipeline if demand arrives before the app ships
- **Deliverable:** Second distribution channel. Highest-ROI channel for legal audience.

### Phase 8 — MCP Endpoint
**Goal:** Structured query interface for power users.
- API design: query by FOLIO concept, date range, significance, court, citation
- Full-text search across cookie summaries
- Cross-reference queries (citation graph)
- MCP protocol implementation
- **Deliverable:** SG Law Cookies as a legal research tool, not just a news feed.

### Phase 9 — Weekly/Monthly Rollups
**Goal:** Auto-generated synthesis of weekly and monthly trends.
- Aggregate stats computation
- Theme and trend identification (LLM-assisted synthesis across the week's cookies)
- Published as special cookie pages on the blog
- Sent to email subscribers
- **Deliverable:** Reference material nobody else produces.

### Phase 10 — Additional Channels & Richer Personalisation
**Goal:** Broader distribution and smarter recommendations.
- Telegram bot with topic-subscribed push notifications
- LinkedIn auto-posting
- Personalisation overlay on constellation
- Usage-based feedback loop for significance tuning
- Entity-based matching beyond area-of-law subscriptions
- **Deliverable:** Full platform maturity.

---

## 10. Open Questions

### Prompt Design
- What is the optimal prompt structure for judgment structural
  extraction? Needs testing against real eLitigation output across
  different courts and lengths.
- How should the cookie generation prompt handle judgments with
  one standout issue vs. multiple equally significant issues?

### FOLIO Coverage
- How complete is FOLIO's coverage of Singapore-specific legal
  concepts, courts, and statutory bodies?
- At what point does the local mapping table for unresolved
  Singapore terms warrant a formal FOLIO extension contribution?

### Volume, Freshness and Cost
- What is the actual daily judgment volume in `zeeker-judgements`?
  Estimated at 5–30, but needs empirical measurement.
- How quickly do new judgments and headlines appear in Zeeker after
  publication? The daily cookie's freshness is bounded by Zeeker's
  own update cadence.
- How does cost scale with judgment complexity? The long-judgment
  path (5–8 LLM calls) needs cost monitoring — and note that
  issue-level summarisation is one call *per issue*, so a
  many-issue judgment can exceed the 8-call estimate.

### Design
- Direction settled (§6.1): bakery-shopfront on Zeeker tokens,
  approved mockup `mockups/f-bakery-lovecon.html`. Remaining:
  whether to engage a designer for identity polish (logo, signboard
  lettering) once the built site is live, and how the constellation
  page adopts the shopfront vocabulary.

### Visualisation
- What is the right balance between information density and
  visual clarity at Level 2 of the constellation, especially
  on busy days with 100+ cookies?
- Should the time navigation show smooth transitions or discrete
  day-by-day steps?

### Legal and Ethical
- Zeeker is a catalogue and does not reproduce source content; the
  mixed licence labels in its metadata describe the underlying
  sources' terms. The cookies app follows the same model (derived
  text + outbound links, no republication), which should keep it
  clear of those terms — sanity-check this holds for the
  restrictively labelled databases (`pdpc`, `sg-gov-newsrooms`),
  and confirm the §2.5 attribution format satisfies CC-BY where it
  applies (`zeeker-judgements`, `sglawwatch`).
- Disclaimer positioning: cookies are LLM-generated signals,
  not legal advice. How prominently should this be surfaced?

### Carried-Forward Gaps
- **Source merging.** Dedup currently flags duplicates but never
  merges a second source into an existing cookie, so the
  many-to-many source↔cookie promise (§2.1) is unimplemented.
  Define when a duplicate becomes a corroborating `source_ids`
  entry instead.
- **Quality and evals.** No accuracy validation or golden test set
  for LLM extraction yet. For legal signals, hallucinated holdings
  are the worst failure mode; this needs a real strategy, not just
  a disclaimer.
- **Embedding model** for sqlite-vec dedup is unchosen and
  uncosted.
- **Backfill.** Zeeker's historical data makes backfilling the
  constellation's time scrubber feasible — decide how far back to
  process on launch.
- **Success metrics and non-goals** are not yet defined.

---

## Appendix A — Sample News Extraction Prompt

See the tested prompt from earlier in the design process. Key design
principles carried forward:

- Use tool use / structured outputs to enforce JSON schema (not
  free-text JSON)
- Closed vocabulary for areas_of_law replaced by FOLIO resolution
  step (LLM extracts free text, FOLIO normalises)
- Multi-topic splitting: one cookie per distinct legal proposition
- Significance rubric with concrete definitions per level
- Anti-hallucination and anti-verbatim-copying rules
- Audience framing: Singapore-qualified lawyer, pressed for time

The prompt produces `TopicExtraction` objects with `raw_areas`,
`raw_entities`, and `raw_concepts` as free text. FOLIO resolution
then converts these to `FolioRef` objects with standardised IRIs.

## Appendix B — Enrichment Pipeline Pseudocode

Full pseudocode for both pipelines, FOLIO resolution, daily
orchestrator, and notification filtering is maintained as a separate
document: `enrichment-pipeline-pseudocode.md`.

Note: the pseudocode predates the Zeeker and external-subscription
decisions. Its collector functions are superseded by the Zeeker
client (§3.2 Layer 1), and its notification filter now describes
logic owned by the external subscription app (§6.2).
