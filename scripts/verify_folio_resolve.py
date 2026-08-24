"""Phase 1 verification: re-resolve cookies and compare against stored values.

Picks a sample of cookies from the live database, re-resolves their
raw labels through the current folio-resolve adapter, and compares
the output against the stored FolioRefs.

Usage:
    uv run python scripts/verify_folio_resolve.py [--limit N]
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sg_law_cookies import folio
from sg_law_cookies.models import TopicExtraction


def _ref_dict(ref) -> dict:
    return {
        "iri": ref.iri,
        "preferred_label": ref.preferred_label,
        "branch": ref.branch,
        "confidence": round(ref.confidence, 4),
    }


def _stored_refs(raw: str) -> list[dict]:
    """Parse stored FolioRef JSON into dicts."""
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        return []
    return data


def _labels_from_stored(raw: str) -> list[str]:
    """Extract preferred_labels from stored FolioRef JSON."""
    refs = _stored_refs(raw)
    return [r.get("preferred_label", "") for r in refs if isinstance(r, dict) and r.get("preferred_label")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--db", default="cookies.db")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT * FROM cookies
           WHERE folio_concepts != '[]' OR folio_entities != '[]'
           ORDER BY RANDOM() LIMIT ?""",
        (args.limit,),
    ).fetchall()

    if not rows:
        print("No cookies with FOLIO tags found.")
        return

    print(f"Verifying {len(rows)} cookies (stored vs re-resolved)")
    print("=" * 80)

    matches = 0
    differences = 0
    errors = 0

    with httpx.Client(timeout=30.0) as client:
        for i, row in enumerate(rows):
            folio.clear_cache()

            area_labels = _labels_from_stored(row["folio_areas"])
            entity_labels = _labels_from_stored(row["folio_entities"])
            concept_labels = _labels_from_stored(row["folio_concepts"])

            if not area_labels and not entity_labels and not concept_labels:
                continue

            topic = TopicExtraction(
                headline=row["headline"],
                summary=row["summary"],
                why_it_matters=row["why_it_matters"],
                significance=row["significance"],
                raw_areas=area_labels,
                raw_entities=entity_labels,
                raw_concepts=concept_labels,
            )

            try:
                folio.resolve_topic(topic, client)
            except Exception as e:
                errors += 1
                print(f"  [{i+1}/{len(rows)}] ERROR: {e}  {row['headline'][:60]}")
                continue

            stored_areas = _stored_refs(row["folio_areas"])
            stored_entities = _stored_refs(row["folio_entities"])
            stored_concepts = _stored_refs(row["folio_concepts"])

            new_areas = [_ref_dict(r) for r in topic.folio_areas]
            new_entities = [_ref_dict(r) for r in topic.folio_entities]
            new_concepts = [_ref_dict(r) for r in topic.folio_concepts]

            # Compare by IRI + label (ignore branch/confidence differences)
            def _key(refs):
                return {(r.get("iri"), r.get("preferred_label")) for r in refs}

            same = (
                _key(stored_areas) == _key(new_areas) and
                _key(stored_entities) == _key(new_entities) and
                _key(stored_concepts) == _key(new_concepts)
            )

            if same:
                matches += 1
                status = "SAME"
            else:
                differences += 1
                status = "DIFF"
                diff_fields = []
                if _key(stored_areas) != _key(new_areas):
                    diff_fields.append("areas")
                if _key(stored_entities) != _key(new_entities):
                    diff_fields.append("entities")
                if _key(stored_concepts) != _key(new_concepts):
                    diff_fields.append("concepts")
                status += f" ({', '.join(diff_fields)})"

            print(f"  [{i+1}/{len(rows)}] {status}  {row['headline'][:60]}")

    print("=" * 80)
    total = matches + differences + errors
    pct = (matches / total * 100) if total else 0
    print(f"Total: {total} cookies — {matches} same ({pct:.0f}%), {differences} diff, {errors} errors")

    if differences:
        print(f"\nNote: diffs are expected — stored cookies were resolved by the legacy")
        print(f"engine. The new engine may resolve differently (better gates, word-order")
        print(f"invariant scoring). Manual review of diffs is needed.")


if __name__ == "__main__":
    main()