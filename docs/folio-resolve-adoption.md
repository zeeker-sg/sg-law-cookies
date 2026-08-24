# folio-resolve Adoption Plan

## Context

### What this is

[folio-resolve](https://github.com/damienriehl/folio-resolve) is an MIT-licensed,
pure-Python library (pydantic-only core) that maps arbitrary source text to
FOLIO ontology concepts — the same 18,000+ concept ontology this project uses
(PRD §4.3). It is the shared engine extracted from three repos (folio-mapper,
folio-enrich, folio-insights) that had independently built and drifted apart
on the same matching logic. v0.4.0, 217 commits, published on PyPI.

### Why we want it

Our current FOLIO resolution lives in `src/sg_law_cookies/folio.py` (~350 lines)
and `src/sg_law_cookies/sg_mappings.py` (~110 lines). It works, but has known
weaknesses that folio-resolve solves properly:

| Current pain | Where in our code | folio-resolve solution |
|---|---|---|
| FOLIO API fuzzy scorer has a ~90 junk floor; we hack around it by requiring shared tokens | `folio.py:54-59` `_normalised_relevance()` | **Score calibration** (verdict-labeled P(correct) fit) + **place-name/short-label gates** |
| Generic court names substring-match US courts at 0.8 confidence ("Court of Appeal" → "Washington Court of Appeals") | `sg_mappings.py:36-49`, `folio.py:259-280` | **PlaceNameGate** with consumer vocabulary + **alias/homonym blocklist** |
| No handling for compound headings ("Proposed Findings of Fact and Conclusions of Law") | not handled | **Span decomposition** (conjunction split + shared head/tail) |
| Singular/plural mismatch ("agreement" vs "Agreements") | not handled | **Lemma-key index augmentation** (build-time spaCy, cached JSON) |
| Exact 1.0 / substring 0.8 / else API relevance — simple, no word-order invariance | `folio.py:116-146` `pick_best_match()` | **Word-order-invariant scorer** ("arbitration rules" = "rules of arbitration"), Aho-Corasick entity ruler |
| Singapore-specific entities maintained as a separate hardcoded table | `sg_mappings.py` | **Domain-prior** mechanism (auto-suggest + validate/add), structured local mapping |
| No disambiguation for homonyms (Action ≠ Auction) | not handled | **Alias/homonym blocklist** (deterministic guard) |

### Design fit

- **Same philosophy.** PRD §4.3 separates FOLIO resolution as a
  "deterministic post-processing step, not part of the LLM prompt."
  folio-resolve is designed as exactly that.
- **Same stack.** Python, pydantic, uv. Pure-Python core = minimal dependency
  footprint. Optional extras (`[folio]`, `[embedding]`, `[spacy]`) behind
  Protocol seams.
- **Determinism guarantee.** Byte-identical output across processes (tested
  under different `PYTHONHASHSEED`). Reproducible cookies.
- **Key-agnostic (BYOK).** The library never reads an env var or makes a
  network call on its own. The zero-key deterministic core (ruler, scoring,
  decomposition, gates, blocklist, calibration) runs fully offline. Optional
  stages (Judge, Embeddings, DomainPriorSuggester) accept a provider through
  Protocol seams.
- **Singapore gap acknowledged.** folio-resolve's domain-prior mechanism is
  the structured way to handle Singapore-specific entities, rather than a
  parallel hardcoded table.

---

## Scope

### In scope

- Replace `folio.py`'s search/match layer with folio-resolve's `MatchPipeline`
- Port `sg_mappings.py` entries into folio-resolve's domain-prior / local
  mapping mechanism
- Keep `area_vocab.py` as-is (closed vocabulary for LLM extraction is upstream
  of resolution)
- Keep `FolioRef` as our data model; write an adapter between folio-resolve
  result types and `FolioRef`
- Run behind a feature flag for side-by-side comparison during migration

### Out of scope (for now)

- `[embedding]` extra (sentence-transformers / FAISS for semantic recall) —
  revisit when implementing embedding-based dedup (PRD §4.1 Step 3)
- `[spacy]` extra (lemma-key augmentation) — revisit if singular/plural
  mismatch becomes a real problem
- `[folio]` extra (folio-python live ontology adapter) — we currently use
  the REST API; evaluate whether loading the ontology in-memory is better
- LLM judge stage — our existing Anthropic/Ollama backends could fill the
  `Judge` Protocol, but this adds per-resolution LLM cost; defer

---

## Phases

### Phase 0 — Spike ✅ (completed)

**Goal:** Validate the library works for our data before committing to a
full migration. Behind a feature flag, no production changes.

**What was done:**
1. `uv add folio-resolve` (core, pure-Python)
2. Wrote `src/sg_law_cookies/folio_resolve_adapter.py` with two approaches:
   - **Attempt 1 (bulk fetch):** `InMemoryOntology` populated from REST API
     at startup. Failed — FOLIO API returns 429 when bulk-fetching 8 branches.
     4/10 cookies matched, 6/10 different (all unresolved due to empty ontology).
   - **Attempt 2 (Option C — hybrid):** Custom `OntologyProvider` that wraps
     the FOLIO REST API per-query (same network pattern as legacy), feeds
     results through `MatchPipeline`'s gates, blocklist, and scoring.
     **7/10 cookies matched**, 3/10 different (branch metadata missing,
     rate-limiting, one false positive).
3. Added `FOLIO_RESOLVE=1` env flag to `config.py` with delegation hooks
   in `folio.py` (zero behavioral change without the flag)
4. 15 adapter-specific tests with in-memory ontology (all passing)
5. Side-by-side comparison script: `scripts/compare_folio_resolve.py`
6. Comparison report: `docs/folio-resolve-comparison.json`

**Key findings:**
- **Option C (hybrid) is the right approach.** No bulk ontology fetch needed.
  Keep per-query REST API, add folio-resolve's gates/scoring on top.
- **Branch metadata gap:** Provider returns `branch=""` because it doesn't
  call `/taxonomy/tree/path/<id>`. Need lazy branch resolution.
- **Rate limiting:** Both legacy and adapter hit 429s. Pre-existing issue.
  Solution: share cache or add rate limiting.
- **Score floor:** One false positive ("Law Minister" → "NIST" @ 0.9).
  Tuning score_floor or adding LLM judge would help.

### Phase 1 — Core migration

**Goal:** Replace the search/match layer in `folio.py` with folio-resolve.
Remove the feature flag — make folio-resolve the default.

1. **Add lazy branch resolution** to `RestApiOntologyProvider` — call
   `/taxonomy/tree/path/<id>` per IRI (cached), same as legacy `_branch_for_iri`
2. **Merge the adapter into `folio.py`** — the adapter becomes the
   implementation, not a sidecar behind a flag. Remove the delegation
   hooks and the `FOLIO_RESOLVE` env flag.
3. **Delete legacy functions:** `_search_all_branches()`, `_search_branch()`,
   `_normalised_relevance()`, `pick_best_match()`, `SearchResult`. The
   `RestApiOntologyProvider` + `MatchPipeline` replace them.
4. **Keep `sg_mappings.py`** as-is — it runs before the pipeline and
   handles Singapore-specific entities. No need to port to domain-prior
   yet (that's Phase 2).
5. **Keep `area_vocab.py`** as-is — closed vocabulary lookup, upstream
   of resolution.
6. **Update tests:** Legacy respx-mocked tests need to mock the provider's
   API calls instead of the old functions. Adapter tests already pass.
7. **Pin folio-resolve version** in `pyproject.toml` (e.g. `==0.4.0`).

**Exit criteria:**
- All existing tests pass (updated for the new API call pattern)
- `folio.py` is simpler (search/match logic delegated to folio-resolve)
- No feature flag — folio-resolve is the default
- Side-by-side comparison shows ≥90% SAME or better vs legacy

### Phase 2 — Capability adoption (optional, incremental)

Each capability is independent and can be adopted when needed:

- **Span decomposition** — enable for judgment pipeline where compound legal
  headings are common. Wire `decompose()` into the concept resolution pass.
- **Lemma-key augmentation** — adopt `[spacy]` extra if singular/plural
  mismatch surfaces as unresolved terms. Build-time only; steady-state loads
  cached JSON.
- **Score calibration** — once we have enough verdict-labeled data, fit a
  calibration curve to replace the hard 0.6 threshold.
- **LLM judge** — wire our Anthropic/Ollama backend into the `Judge`
  Protocol for context-aware disambiguation on low-confidence matches.
  Adds per-resolution LLM cost; use selectively.
- **Embedding semantic path** — adopt `[embedding]` extra when implementing
  embedding-based dedup (PRD §4.1 Step 3). Also improves recall for
  no-shared-token maps.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Young library (v0.4.0, single maintainer)** | MIT-licensed; we can pin a version and fork if needed. Pin in `pyproject.toml`, not a floating range. |
| **US-centric design** | Singapore entities handled via domain-prior mechanism. Our `sg_mappings.py` knowledge carries over. |
| **Ontology loading pattern change** | Currently REST API per query; folio-resolve entity ruler needs labels in-memory. Use `InMemoryOntology` loaded from a cached snapshot, refreshed periodically. Evaluate `[folio]` extra (folio-python) for live access. |
| **Complexity increase** | folio-resolve is a full engine (15+ modules). Keep our public interface (`resolve_topic`, `resolve_judgment_meta`) unchanged so the complexity stays inside the adapter. |
| **Determinism assumptions** | folio-resolve guarantees byte-identical output. Our pipeline benefits, but we need to ensure our adapter preserves total ordering on ties (IRI, label). |

---

## Open questions (resolved by Phase 0)

1. **Ontology loading:** ~~Load from REST API snapshot or use `[folio]` extra?~~
   **Resolved:** Neither. Option C — keep per-query REST API, wrap in a
   custom `OntologyProvider`. No bulk fetch, no in-memory ontology.

2. **Domain-prior mechanism:** ~~Should `sg_mappings.py` become `DomainPrior`?~~
   **Resolved:** Keep `sg_mappings.py` as-is for Phase 1. It runs before
   the pipeline. Domain-prior adoption deferred to Phase 2.

3. **Branch routing:** ~~How to map venue/legislation branch filters?~~
   **Resolved:** Use `_resolve_branch_filtered()` for venue/legislation
   (branch-filtered `/search/query` + folio-resolve scoring). Use
   `MatchPipeline.match()` for concepts/entities (all-branches
   `/search/label` + gates).

4. **`FolioRef` adapter shape:** ~~Score normalisation?~~
   **Resolved:** folio-resolve scores 0-100 → `FolioRef.confidence` 0.0-1.0.
   Divide by 100.