"""Local mappings for legal areas not in FOLIO's area-of-law taxonomy.

FOLIO's area_of_law branch is US-centric and lacks several legal domains
that are important in Singapore practice (arbitration, ethics, dispute
resolution, etc.).  These areas are mapped locally so the LLM's raw_area
labels resolve to a FolioRef with ``branch="sg_local_area"`` instead of
landing in ``unresolved``.

The extraction LLM is constrained to the closed vocabulary in
``area_vocab.py`` (161 labels harvested from FOLIO).  Labels that are NOT
in that vocabulary go to ``unresolved`` in ``resolve_topic`` pass 1.
Adding a label here registers a local fallback so those labels still
produce a tagged area instead of an unresolved entry.

To add a new area: call ``_register_area("Label")`` with any aliases.
The label must match exactly what the LLM might emit as a ``raw_area``.
"""

from __future__ import annotations

from sg_law_cookies.models import FolioRef

SG_LOCAL_AREA_BRANCH = "sg_local_area"

_LOCAL_AREAS: dict[str, FolioRef] = {}


def _norm(term: str) -> str:
    return " ".join(term.replace("’", "'").lower().split()).strip(".")


def _register_area(label: str, *aliases: str) -> None:
    ref = FolioRef(
        iri=None,
        preferred_label=label,
        branch=SG_LOCAL_AREA_BRANCH,
        confidence=1.0,
    )
    for key in (label, *aliases):
        _LOCAL_AREAS[_norm(key)] = ref


# ── Areas missing from FOLIO's area-of-law taxonomy ──────────────
# Each label below was verified absent from the 161-label FOLIO snapshot
# (rechecked against the live API 2026-08-28).  FOLIO genuinely does not
# model these practice areas.  Labels already in FOLIO (Energy Law,
# Islamic Law, Sports Law) are NOT duplicated here — the closed-vocab
# lookup in resolve_topic pass 1 catches them first.

_register_area("Arbitration Law", "International Arbitration Law")
_register_area("Ethics and Professional Responsibility Law")
_register_area("Dispute Resolution Law", "Alternative Dispute Resolution Law", "ADR Law")
_register_area("Mediation Law")
_register_area("Technology, Media, and Telecommunications Law", "TMT Law")
_register_area("Space Law")
_register_area("Climate Law", "Climate Change Law")
_register_area("Fashion Law")


def lookup_local_area(term: str) -> FolioRef | None:
    """Return a local FolioRef for a known non-FOLIO area, else None."""
    ref = _LOCAL_AREAS.get(_norm(term))
    return ref.model_copy() if ref else None