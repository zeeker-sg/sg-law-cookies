"""Phase 0 side-by-side comparison: legacy folio.py vs folio-resolve adapter.

Picks a sample of cookies from the live database, re-resolves their
raw labels through both paths, and diffs the FolioRef outputs.

Usage:
    uv run python scripts/compare_folio_resolve.py [--limit N] [--output FILE]

Requires FOLIO_RESOLVE to be unset (legacy) and then set (adapter) — the
script toggles it internally.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path

import httpx

# Ensure we can import the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sg_law_cookies.folio import (
    resolve_topic as legacy_resolve_topic,
    resolve_judgment_meta as legacy_resolve_judgment_meta,
    clear_cache as legacy_clear_cache,
)
from sg_law_cookies.models import TopicExtraction, FolioRef, JudgmentMeta, JudgmentIssue


def _ref_dict(ref: FolioRef) -> dict:
    return {
        "iri": ref.iri,
        "preferred_label": ref.preferred_label,
        "branch": ref.branch,
        "confidence": round(ref.confidence, 4),
    }


def _topic_from_cookie(row: sqlite3.Row) -> TopicExtraction:
    """Reconstruct a TopicExtraction from a stored cookie's raw fields."""
    raw_areas = json.loads(row["folio_areas"]) if row["folio_areas"] else []
    raw_entities = json.loads(row["folio_entities"]) if row["folio_entities"] else []
    raw_concepts = json.loads(row["folio_concepts"]) if row["folio_concepts"] else []

    # We need the RAW labels (pre-resolution), not the resolved FolioRefs.
    # Extract raw labels from the resolved refs (preferred_label is the
    # label the LLM extracted, or the FOLIO label it resolved to).
    # For a fair comparison, we use the resolved preferred_labels as
    # the raw input — this means we're comparing how each path resolves
    # the same labels.
    area_labels = [r.get("preferred_label") if isinstance(r, dict) else r for r in (raw_areas if isinstance(raw_areas, list) else [])]
    entity_labels = [r.get("preferred_label") if isinstance(r, dict) else r for r in (raw_entities if isinstance(raw_entities, list) else [])]
    concept_labels = [r.get("preferred_label") if isinstance(r, dict) else r for r in (raw_concepts if isinstance(raw_concepts, list) else [])]

    # Filter out None labels and unresolved placeholders
    area_labels = [l for l in area_labels if l and isinstance(l, str)]
    entity_labels = [l for l in entity_labels if l and isinstance(l, str)]
    concept_labels = [l for l in concept_labels if l and isinstance(l, str)]

    return TopicExtraction(
        headline=row["headline"],
        summary=row["summary"],
        why_it_matters=row["why_it_matters"],
        significance=row["significance"],
        raw_areas=area_labels,
        raw_entities=entity_labels,
        raw_concepts=concept_labels,
    )


def _resolve_legacy(topic: TopicExtraction, client: httpx.Client) -> TopicExtraction:
    legacy_clear_cache()
    return legacy_resolve_topic(topic, client)


def _resolve_adapter(topic: TopicExtraction, client: httpx.Client) -> TopicExtraction:
    os.environ["FOLIO_RESOLVE"] = "1"
    # Force reimport of the adapter
    from sg_law_cookies.folio_resolve_adapter import (
        resolve_topic as adapter_resolve,
        set_pipeline,
    )
    set_pipeline(None)  # clear any cached pipeline
    result = adapter_resolve(topic, client)
    os.environ.pop("FOLIO_RESOLVE", None)
    set_pipeline(None)  # clear for next run
    return result


def _refs_equal(a: list[FolioRef], b: list[FolioRef]) -> bool:
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        if ra.iri != rb.iri or ra.preferred_label != rb.preferred_label:
            return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20, help="sample size")
    parser.add_argument("--db", default="cookies.db")
    parser.add_argument("--output", default=None, help="write report to file")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Pick cookies that have FOLIO concepts or entities (interesting ones)
    rows = conn.execute(
        """SELECT * FROM cookies
           WHERE folio_concepts != '[]' OR folio_entities != '[]'
           ORDER BY RANDOM() LIMIT ?""",
        (args.limit,),
    ).fetchall()

    if not rows:
        print("No cookies with FOLIO tags found.")
        return

    print(f"Comparing {len(rows)} cookies (legacy vs folio-resolve adapter)")
    print("=" * 80)

    with httpx.Client(timeout=30.0) as client:
        differences = []
        errors = []
        t0 = time.time()

        for i, row in enumerate(rows):
            topic = _topic_from_cookie(row)
            if not topic.raw_areas and not topic.raw_entities and not topic.raw_concepts:
                continue

            # Legacy
            try:
                legacy_topic = _resolve_legacy(
                    TopicExtraction(
                        headline=topic.headline, summary=topic.summary,
                        why_it_matters=topic.why_it_matters,
                        significance=topic.significance,
                        raw_areas=list(topic.raw_areas),
                        raw_entities=list(topic.raw_entities),
                        raw_concepts=list(topic.raw_concepts),
                    ),
                    client,
                )
            except Exception as e:
                errors.append(f"[{i}] legacy error: {e}")
                continue

            # Adapter
            try:
                adapter_topic = _resolve_adapter(
                    TopicExtraction(
                        headline=topic.headline, summary=topic.summary,
                        why_it_matters=topic.why_it_matters,
                        significance=topic.significance,
                        raw_areas=list(topic.raw_areas),
                        raw_entities=list(topic.raw_entities),
                        raw_concepts=list(topic.raw_concepts),
                    ),
                    client,
                )
            except Exception as e:
                errors.append(f"[{i}] adapter error: {e}")
                continue

            # Diff
            diffs = []
            if not _refs_equal(legacy_topic.folio_areas, adapter_topic.folio_areas):
                diffs.append("areas")
            if not _refs_equal(legacy_topic.folio_entities, adapter_topic.folio_entities):
                diffs.append("entities")
            if not _refs_equal(legacy_topic.folio_concepts, adapter_topic.folio_concepts):
                diffs.append("concepts")
            if set(legacy_topic.unresolved) != set(adapter_topic.unresolved):
                diffs.append("unresolved")

            if diffs:
                differences.append({
                    "index": i,
                    "headline": row["headline"][:80],
                    "raw_areas": topic.raw_areas,
                    "raw_entities": topic.raw_entities,
                    "raw_concepts": topic.raw_concepts,
                    "diff_fields": diffs,
                    "legacy_areas": [_ref_dict(r) for r in legacy_topic.folio_areas],
                    "adapter_areas": [_ref_dict(r) for r in adapter_topic.folio_areas],
                    "legacy_entities": [_ref_dict(r) for r in legacy_topic.folio_entities],
                    "adapter_entities": [_ref_dict(r) for r in adapter_topic.folio_entities],
                    "legacy_concepts": [_ref_dict(r) for r in legacy_topic.folio_concepts],
                    "adapter_concepts": [_ref_dict(r) for r in adapter_topic.folio_concepts],
                    "legacy_unresolved": legacy_topic.unresolved,
                    "adapter_unresolved": adapter_topic.unresolved,
                })

            status = "SAME" if not diffs else f"DIFF: {', '.join(diffs)}"
            print(f"  [{i+1}/{len(rows)}] {status}  {row['headline'][:60]}")

    elapsed = time.time() - t0
    print("=" * 80)
    print(f"Total: {len(rows)} cookies, {len(differences)} differences, {len(errors)} errors, {elapsed:.1f}s")

    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  {e}")

    if differences:
        print(f"\n{'='*80}")
        print("Detailed differences:")
        print(f"{'='*80}")
        for d in differences:
            print(f"\n--- [{d['index']}] {d['headline']} ---")
            print(f"  Raw areas: {d['raw_areas']}")
            print(f"  Raw entities: {d['raw_entities']}")
            print(f"  Raw concepts: {d['raw_concepts']}")
            print(f"  Diff fields: {d['diff_fields']}")
            if "areas" in d["diff_fields"]:
                print(f"  Legacy areas:   {d['legacy_areas']}")
                print(f"  Adapter areas:  {d['adapter_areas']}")
            if "entities" in d["diff_fields"]:
                print(f"  Legacy entities:  {d['legacy_entities']}")
                print(f"  Adapter entities: {d['adapter_entities']}")
            if "concepts" in d["diff_fields"]:
                print(f"  Legacy concepts:  {d['legacy_concepts']}")
                print(f"  Adapter concepts: {d['adapter_concepts']}")
            if "unresolved" in d["diff_fields"]:
                print(f"  Legacy unresolved:  {d['legacy_unresolved']}")
                print(f"  Adapter unresolved: {d['adapter_unresolved']}")

    # Write report
    report = {
        "total": len(rows),
        "differences": len(differences),
        "errors": len(errors),
        "elapsed_s": round(elapsed, 1),
        "diffs": differences,
        "errors_list": errors,
    }
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
        print(f"\nReport written to {args.output}")
    else:
        # Default output location
        out = Path("docs/folio-resolve-comparison.json")
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()