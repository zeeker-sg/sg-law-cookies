# Story foundation: compatibility first

This change is structural. It does not group stories, rewrite published content,
change admission policy, install a model, or expose additional source content.

## Records and ownership

The legacy tables remain the writable compatibility surface. Every successful
commit also updates the structural records in the same SQLite transaction:

| Record | Meaning |
| --- | --- |
| `stories` | A provisional reader-facing story, initially one per legacy cookie; current presentation pointer, recorded admission, nullable event/meaningful-update clocks. |
| `insights`, `insight_versions` | Stable legacy proposition ID and immutable captured versions, including exact text, significance and FOLIO JSON. |
| `documents`, `document_identifiers` | One bookkeeping identity per retained Source, recorded collector/URL identity; no inferred document equivalence. |
| `input_observations`, `source_observation_heads` | Immutable exact observed input and provenance; latest recorded observation pointer, not a “best source” decision. Source enrichment retains previous input and evidence versions. |
| `insight_evidence` | Version-specific input associations. Migration creates `legacy_association`, never newly verified support. |
| `presentation_revisions` | Captured presentation, insight-version references and source union. Baseline is `migration_capture`, not original wording or reconstructed approval. |
| `legacy_identities` | Existing cookie/insight ID, provisional story mapping and recorded identity/day keys. Existing readers continue using the unchanged legacy fields. |
| `review_events`, `write_events` | Private append-only before/after histories, including deleted/regenerated drafts and metadata writes. |
| `processing_receipts` | Successful complete input/output batches, linked to observed input, pipeline and configured extractor identifier. Legacy completeness is unknown: no receipts are fabricated. |
| `relationship_assessments` | Fixed document/story/insight vocabularies with status, rationale and actor. Empty after migration; no semantic relation writer or automatic grouping is introduced. |

Story IDs are deterministic internal UUIDs; public cookie IDs remain unchanged.
There is no oldest/latest/source-authority presentation selection rule. Every
legacy cookie has its own provisional story, including existing duplicate flags;
the legacy filters remain authoritative for public visibility.

Input kind, selected field/projection, upstream version and available clocks are
recorded for new observations. Legacy values stay NULL, including historical date
roles and document authority. `sources.date` is preserved as recorded even where
an earlier fallback may have mixed publication/decision and discovery dates.
Event time and meaningful-update time remain NULL; this release never bumps a
feed for source-count growth or taxonomy changes. FOLIO changes do create an
immutable captured version, preserving what the reader previously saw.

## Writer contract

`db.init_db` returns a version-aware connection. Existing schema-zero databases
require an explicit migration; normal startup refuses to migrate them silently.
Future schema versions are rejected. SQLite triggers reject ordinary legacy
writers that do not register this runtime's guard functions. This is an
application-integrity guard, not a security boundary against a database owner.

All database writer helpers use nested savepoints. Inner commits are deferred;
the outer commit flushes dirty sources first, then complete cookie revisions.
The same commit retains the source union and updates all current pointers.
Audited direct SQL through the new connection is also journaled and flushed;
`executescript` is prohibited outside schema initialization because it implicitly
commits. Published identities/source links cannot be deleted through this facade.

`Cookie.revision` is an internal optimistic token, excluded from public model
serialization. Published text/FOLIO replacement checks the token under the write
lock. Source-link additions are union operations. Draft edits/regeneration use
fingerprints; Discord modal IDs carry full tokens within the 100-character cap.
Message-ID bookkeeping does not invalidate a content fingerprint. Old in-flight
modals must be reopened. Old approval cards retain IDs and are accepted only when
their visible prose matches the current draft; stale cards require refresh.

News and judgment model work occurs before acquiring the store transaction.
Source storage, all generated outputs, metadata and a completion receipt then
commit together. A failed batch leaves no Source that would incorrectly cause
the next retry to skip it. Regeneration preserves the old draft in private
history and replaces it atomically only if its fingerprint still matches.

Existing-URL skip behaviour and admission rules remain unchanged. Reprocessing
legacy sources with unknown completeness or richer observations requires a later
explicit policy; absence of a legacy receipt is not permission for paid inference.
The new receipt records configured extractor identity, not a claim to reconstruct
historical prompts or model versions.

## Migration and rehearsal

Use the existing runtime and private paths:

```
cookies migrate-schema --source /private/baseline.db --out /private/migrated.db
python scripts/rehearse_schema.py --source /private/baseline.db --out /private/new-rehearsal
```

Both commands create new files/directories and refuse overwrites. The copy uses
SQLite's backup API, preserving committed WAL data; outputs are private. The
original database is never replaced. Migration uses transactional DDL, seeds
complete one-to-one mappings and capture histories, then installs guards. It
does not populate semantic assessments or processing receipts. A second migration
is a no-op. A future schema upgrade must explicitly rebuild versioned triggers;
changing the Python version constant alone is insufficient.

Reconciliation compares exact logical column/row sets for every legacy table,
including raw text, JSON formatting, IDs, source associations, watermarks, daily
stats and all review fields. Rehearsal renders both copies with the same code and
uses one explicit export build clock. This makes even `_zeeker_updates` metadata
comparable. Production exports still use the actual current build clock.

Public export retains its existing allowlist and gains a consistent read
transaction across public tables. None of the new private tables is exported.
No Datasette/MCP schema, taxonomy, API URL, daily route, sky identity or Atom tag
change is needed for this foundation. Semantic story display and a versioned
story API are later work.

## Backup, restore and rollback

Backups retain the private `backups/sg-law-cookies/` keys, never public `latest/`.
They validate the structural contract before uploading. A cold restore validates
schema, guards, integrity and foreign keys. A running database that is logically
identical is left alone, including its WAL sidecars. A divergent canonical/local
pair is refused: online file replacement cannot fence already-open reviewers or
preserve intervening approvals. Legacy direct-overwrite download scripts now use
this same guarded restore path.

`prepare_rollback` only produces a candidate copy. It refuses intervening writes
using the full logical manifest, including metadata and private history. It never
replaces a live file. After live writes, reconcile or roll forward; restoring an
older snapshot would lose those writes. Append-only history increases private
database size; no pruning or vacuum policy is silently introduced here.

## Proposed production cutover — separate approval required

1. Review/commit/publish the branch through normal CI and protections after the
   user authorizes that stage. No production rollout is implied by local tests.
2. Before cutover, freeze every ingestion, approval, comment, regeneration,
   backfill, restore and publication writer; account for already-open processes.
   Take a fresh WAL-safe backup and code/public-artifact manifests. Preserve the
   pre-migration canonical backup under a private rollback key.
3. Rehearse again from that final snapshot. Require exact legacy equivalence,
   unknown-provenance preservation, integrity/FK checks, and public/rendered
   comparisons. Do not apply any semantic grouping or content revision.
4. While the freeze remains enforced, install the matched new runtime/code and
   migrated database together. Update the private canonical backup to the same
   schema. Do not let an old worker restore or write the new database.
5. Exercise the cycle under controlled conditions: backup, identical canonical
   restore no-op, guarded writer checks, public export/render validation. Resume
   publication and scheduled workers only after those checks pass. Verify the
   actual served public dataset and identity invariants after normal refresh.
6. If anything fails before new writes, restore the matched old code/database/
   canonical/public artifacts while still frozen. After new writes, retain all
   history and reconcile/roll forward; never overwrite them with the old snapshot.

No cutover, credential/access change, model evaluation, source consolidation or
deployment was performed while developing this branch.
