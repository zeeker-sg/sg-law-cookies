#!/usr/bin/env python
"""Snapshot FOLIO's area_of_law branch to src/sg_law_cookies/data/folio_areas.json.

The closed vocabulary the extraction LLM picks from (#4 area-tagging upgrade).
Harvested via the same /search/query?branch=area_of_law endpoint the resolver
uses, so snapshot IRIs match what older runs already stored. Re-run when FOLIO
updates its taxonomy; commit the resulting JSON.
"""

import json
import string
from pathlib import Path

import httpx

BASE = "https://folio.openlegalstandard.org"
BRANCH = "area_of_law"
OUT = Path(__file__).resolve().parents[1] / "src/sg_law_cookies/data/folio_areas.json"


def harvest() -> dict[str, str]:
    """Union every area_of_law class reachable by single-letter substring probes."""
    by_iri: dict[str, dict] = {}
    with httpx.Client(timeout=60) as c:
        for probe in string.ascii_lowercase:
            r = c.get(
                f"{BASE}/search/query",
                params={"label": probe, "branch": BRANCH, "limit": 1000},
            )
            r.raise_for_status()
            for cls in r.json().get("classes", []):
                if cls.get("iri") and cls.get("label"):
                    by_iri[cls["iri"]] = cls
    # label -> iri, keeping the shortest IRI on the (rare) duplicate label
    label_to_iri: dict[str, str] = {}
    for cls in by_iri.values():
        lbl = cls["label"]
        if lbl not in label_to_iri or len(cls["iri"]) < len(label_to_iri[lbl]):
            label_to_iri[lbl] = cls["iri"]
    return dict(sorted(label_to_iri.items()))


def main() -> None:
    vocab = harvest()
    OUT.write_text(json.dumps(vocab, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(vocab)} areas -> {OUT}")


if __name__ == "__main__":
    main()
