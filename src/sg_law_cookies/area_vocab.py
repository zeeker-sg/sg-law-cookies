"""Closed FOLIO area-of-law vocabulary (area-tagging upgrade, option #4).

The extraction LLM selects areas from this fixed set rather than emitting free
text, so every tagged area maps to a real FOLIO concept and resolution is a
local dict lookup with no network call (see folio.resolve_topic pass 1).

The snapshot at data/folio_areas.json is harvested from FOLIO's area_of_law
branch by scripts/refresh_folio_areas.py; regenerate and commit it when the
taxonomy changes.
"""

from __future__ import annotations

import json
from pathlib import Path

_SNAPSHOT = Path(__file__).resolve().parent / "data" / "folio_areas.json"

# label -> IRI, sorted by label (the snapshot is already sorted)
AREA_IRI_BY_LABEL: dict[str, str] = json.loads(_SNAPSHOT.read_text())

# The closed set offered to the model as a JSON-schema enum.
AREA_LABELS: list[str] = list(AREA_IRI_BY_LABEL)
