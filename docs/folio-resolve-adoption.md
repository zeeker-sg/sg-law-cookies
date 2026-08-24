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

### Phase 0 — Spike (1-2 sessions)

**Goal:** Validate the library works for our data before committing to a
full migration. Behind a feature flag, no production changes.

1. `uv add folio-resolve` (core, pure-Python)
2. Write a thin adapter module `src/sg_law_cookies/folio_resolve_adapter.py`:
   - Load ontology labels from our existing `area_vocab.py` + the REST API
     (or a cached snapshot) into `InMemoryOntology`
   - Wrap `MatchPipeline` and expose a `resolve_label(term, branch?) -> FolioRef | None`
     interface that returns our `FolioRef` type
3. Add env flag `FOLIO_RESOLVE=1` to `config.py` that routes resolution
   through the adapter instead of the existing `folio.py` functions
4. Run the existing test suite with `FOLIO_RESOLVE=1` — fix any failures
5. Run a side-by-side comparison: process N recent cookies through both paths,
   diff the `folio_areas`, `folio_entities`, `folio_concepts`, and `unresolved`
   lists. Log where they diverge.

**Exit criteria:**
- Adapter works without errors on real data
- Side-by-side comparison shows folio-resolve is at least as good as current
  on entities/concepts, and better on compound headings / homonyms
- No regressions in the test suite

**Deliverable:** A branch with the adapter, feature flag, and a comparison
report (saved to `docs/folio-resolve-comparison.md` or similar).

### Phase 1 — Core migration (2-3 sessions)

**Goal:** Replace the search/match layer in `folio.py` with folio-resolve.

1. Replace `_search_all_branches()`, `_search_branch()`,
   `_normalised_relevance()`, `pick_best_match()` with
   `MatchPipeline.match()` calls
2. Configure `PlaceNameGate` with Singapore place-name vocabulary
   (Singapore, Johor, etc.) to prevent geographic false positives
3. Port `sg_mappings.py` entries into a folio-resolve domain-prior or local
   mapping layer — the hardcoded table becomes structured config that the
   pipeline consults before hitting the ontology
4. Keep `resolve_topic()` and `resolve_judgment_meta()` as the public
   interface — they call into the adapter internally. Callers in
   `pipeline.py`, `cli.py`, and `judgment.py` should not change.
5. Update tests: mock the ontology instead of the REST API for unit tests;
   keep live API tests behind the `@pytest.mark.live` marker
6. Remove the feature flag once tests pass and comparison is clean

**Exit criteria:**
- All existing tests pass
- `sg_mappings.py` is either removed or reduced to a thin config file
- `folio.py` is significantly simpler (search/match logic delegated to
  folio-resolve)
- A manual spot-check of recent cookies shows correct resolution

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

## Open questions

1. **Ontology loading:** Load from REST API snapshot (cached JSON, refreshed
   per run) or use `[folio]` extra (folio-python live adapter)? Snapshot is
   simpler and deterministic; folio-python is more current but adds a
   dependency.

2. **Domain-prior mechanism:** Should `sg_mappings.py` entries become
   folio-resolve `DomainPrior` config, or stay as a local lookup table that
   runs before the pipeline? Need to understand folio-resolve's domain-prior
   API better (spike will clarify).

3. **Branch routing:** folio-resolve resolves across all branches by default.
   Our current code routes entities to all branches, concepts to all
   branches, venues to `forums_venues`, legislation to `legal_authorities`.
   Need to map this to folio-resolve's branch-filtering mechanism.

4. **`FolioRef` adapter shape:** folio-resolve returns its own result types
   with scores on a 0-100 scale; our `FolioRef.confidence` is 0.0-1.0. Need a
   normalisation step in the adapter.