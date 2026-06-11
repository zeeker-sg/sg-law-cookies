"""Local mapping table for Singapore-specific entities (PRD section 4.3).

FOLIO is US/international-focused and has no concepts for most Singapore
statutory boards, agencies, and courts. These are mapped locally so they
resolve deterministically instead of polluting the unresolved list.
Candidates for a future FOLIO Singapore extension module.
"""

from sg_law_cookies.models import FolioRef

SG_LOCAL_BRANCH = "sg_local"

_SG_ENTITIES: dict[str, FolioRef] = {}


def _norm(term: str) -> str:
    return " ".join(term.replace("’", "'").lower().split()).strip(".")


def _register(label: str, *aliases: str, iri: str | None = None) -> None:
    ref = FolioRef(iri=iri, preferred_label=label, branch=SG_LOCAL_BRANCH, confidence=1.0)
    for key in (label, *aliases):
        _SG_ENTITIES[_norm(key)] = ref


_register("Personal Data Protection Commission", "PDPC")
_register("Building and Construction Authority", "BCA")
_register("Housing and Development Board", "HDB")
_register("Central Provident Fund Board", "CPF Board", "CPF")
_register("Ministry of Manpower", "MOM")
_register("Accounting and Corporate Regulatory Authority", "ACRA")
_register("Attorney-General's Chambers", "AGC")
_register("Intellectual Property Office of Singapore", "IPOS")
_register("Monetary Authority of Singapore", "MAS")
_register("Singapore Exchange", "SGX", "Singapore Exchange Limited")
_register("State Courts", "State Courts of Singapore")
_register("Singapore International Commercial Court", "SICC")
_register("Family Justice Courts", "FJC")
_register(
    "Supreme Court of Singapore",
    "Court of Appeal of Singapore",
    "Singapore Court of Appeal",
)
_register(
    "High Court of Singapore",
    "Singapore High Court",
    "General Division of the High Court",
    "Appellate Division of the High Court",
)
_register("Competition and Consumer Commission of Singapore", "CCCS")
_register("Infocomm Media Development Authority", "IMDA")
_register("Inland Revenue Authority of Singapore", "IRAS")
_register("Urban Redevelopment Authority", "URA")
_register("Singapore Land Authority", "SLA")
_register("Employment Claims Tribunals", "ECT")
_register("Law Society of Singapore")
_register("Singapore Academy of Law", "SAL")


def lookup_sg_entity(term: str) -> FolioRef | None:
    """Return a local FolioRef for a known Singapore entity, else None."""
    ref = _SG_ENTITIES.get(_norm(term))
    return ref.model_copy() if ref else None
