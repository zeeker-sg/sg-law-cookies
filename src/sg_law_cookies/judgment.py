"""Judgment enrichment pipeline (PRD section 4.2, pseudocode section 3).

Ingest -> Triage -> Structural extraction -> Issue-level summarisation
       -> Cookie generation -> FOLIO resolution -> Store

Triage routes by estimated token count: short judgments get a single-pass
extraction, medium ones a two-pass (structure, then one summarisation call
per issue), long ones a chunked extraction with overlap merged back into a
single structure. Cookie generation always runs on the structured digest,
never on the raw text, through the same NEWS_EXTRACTION_TOOL schema as the
news pipeline so both pipelines are interchangeable downstream.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field

import httpx

from sg_law_cookies import db
from sg_law_cookies.extraction import DEFAULT_MODEL, NEWS_EXTRACTION_TOOL
from sg_law_cookies.folio import resolve_judgment_meta, resolve_topic
from sg_law_cookies.llm import as_backend
from sg_law_cookies.models import (
    CaseCitation,
    Cookie,
    FolioRef,
    JudgmentIssue,
    JudgmentMeta,
    RawItem,
    Source,
    TopicExtraction,
)
from sg_law_cookies.prompts_judgment import (
    JUDGMENT_CHUNK_STRUCTURE,
    JUDGMENT_CHUNK_TOOL,
    JUDGMENT_COOKIE_PROMPT,
    JUDGMENT_ISSUE_SUMMARY,
    JUDGMENT_ISSUE_SUMMARY_TOOL,
    JUDGMENT_SINGLE_PASS,
    JUDGMENT_SINGLE_PASS_TOOL,
    JUDGMENT_STRUCTURE,
    JUDGMENT_STRUCTURE_TOOL,
)

logger = logging.getLogger(__name__)

# Triage thresholds (PRD 4.2 step 1), in estimated tokens (len // 4).
SHORT_TOKEN_LIMIT = 5_000
MEDIUM_TOKEN_LIMIT = 20_000

# Chunking for long judgments (pseudocode section 3): ~15k-token chunks
# with ~1k-token overlap, expressed in characters.
CHUNK_SIZE_CHARS = 60_000
CHUNK_OVERLAP_CHARS = 4_000
CHUNK_NUM_CTX = 32_768  # long inputs need a bigger Ollama context window

# Cost guard: issue-level summarisation is one LLM call per issue.
MAX_ISSUES_SUMMARISED = 8

_TREATMENTS = {"followed", "distinguished", "overruled", "referred"}
_UNRESOLVED_BRANCH = "unresolved"
_BACKGROUND_LIMIT = 2_000  # chars, for merged chunk backgrounds


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def triage(token_count: int) -> str:
    if token_count < SHORT_TOKEN_LIMIT:
        return "short"
    if token_count < MEDIUM_TOKEN_LIMIT:
        return "medium"
    return "long"


# ── working structures (pre-FOLIO, with para_refs the models emit) ───


@dataclass
class IssueDraft:
    question: str
    holding: str = ""
    reasoning: str = ""
    raw_concepts: list[str] = field(default_factory=list)
    para_refs: list[str] = field(default_factory=list)


@dataclass
class Structure:
    citation: str = ""
    court_name: str = ""
    judges: list[str] = field(default_factory=list)
    parties: list[str] = field(default_factory=list)
    background: str = ""
    issues: list[IssueDraft] = field(default_factory=list)
    legislation: list[str] = field(default_factory=list)
    cases_cited: list[dict] = field(default_factory=list)  # {citation, treatment}
    orders: str = ""


def _parse_structure(data: dict) -> Structure:
    issues = []
    for item in data.get("issues") or []:
        question = str(item.get("question", "")).strip()
        if not question:
            continue
        issues.append(
            IssueDraft(
                question=question,
                holding=str(item.get("holding", "")),
                reasoning=str(item.get("reasoning", "")),
                raw_concepts=[str(c) for c in item.get("raw_concepts") or []],
                para_refs=[str(p) for p in item.get("para_refs") or []],
            )
        )
    cases = []
    for case in data.get("cases_cited") or []:
        citation = str(case.get("citation", "")).strip()
        if not citation:
            continue
        treatment = case.get("treatment")
        if treatment not in _TREATMENTS:
            treatment = "referred"
        cases.append({"citation": citation, "treatment": treatment})
    return Structure(
        citation=str(data.get("citation", "")).strip(),
        court_name=str(data.get("court_name", "")).strip(),
        judges=[str(j) for j in data.get("judges") or []],
        parties=[str(p) for p in data.get("parties") or []],
        background=str(data.get("background", "")),
        issues=issues,
        legislation=[str(name) for name in data.get("legislation") or []],
        cases_cited=cases,
        orders=str(data.get("orders", "")),
    )


# ── chunking and merge (long path) ───────────────────────────────────


def split_with_overlap(
    text: str,
    chunk_size: int = CHUNK_SIZE_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Contiguous slices of ~chunk_size chars, each overlapping the last."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def _question_tokens(text: str) -> set[str]:
    cleaned = "".join(c if c.isalnum() else " " for c in text.lower())
    return {t for t in cleaned.split() if len(t) >= 3}


def _similar_questions(a: str, b: str) -> bool:
    """Lowercase token-overlap similarity, for deduping issues across chunks."""
    ta, tb = _question_tokens(a), _question_tokens(b)
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def merge_structures(parts: list[Structure]) -> Structure:
    """Reconcile per-chunk extractions into one structure.

    First non-empty citation/court/judges/parties/orders wins; backgrounds
    are concatenated (truncated); issues are deduped by question similarity
    with para_refs unioned; legislation and cases_cited are unioned with
    case-insensitive dedup.
    """
    merged = Structure()
    backgrounds: list[str] = []
    seen_legislation: set[str] = set()
    seen_cases: dict[str, dict] = {}

    for part in parts:
        merged.citation = merged.citation or part.citation
        merged.court_name = merged.court_name or part.court_name
        merged.judges = merged.judges or part.judges
        merged.parties = merged.parties or part.parties
        merged.orders = merged.orders or part.orders
        if part.background.strip():
            backgrounds.append(part.background.strip())

        for issue in part.issues:
            match = next(
                (i for i in merged.issues if _similar_questions(i.question, issue.question)),
                None,
            )
            if match is None:
                merged.issues.append(
                    IssueDraft(
                        question=issue.question,
                        holding=issue.holding,
                        reasoning=issue.reasoning,
                        raw_concepts=list(issue.raw_concepts),
                        para_refs=list(issue.para_refs),
                    )
                )
            else:
                match.holding = match.holding or issue.holding
                match.reasoning = match.reasoning or issue.reasoning
                match.raw_concepts += [
                    c for c in issue.raw_concepts if c not in match.raw_concepts
                ]
                match.para_refs += [p for p in issue.para_refs if p not in match.para_refs]

        for name in part.legislation:
            key = " ".join(name.split()).lower()
            if key and key not in seen_legislation:
                seen_legislation.add(key)
                merged.legislation.append(name)

        for case in part.cases_cited:
            key = " ".join(case["citation"].split()).lower()
            existing = seen_cases.get(key)
            if existing is None:
                seen_cases[key] = case
                merged.cases_cited.append(case)
            elif existing["treatment"] == "referred" and case["treatment"] != "referred":
                existing["treatment"] = case["treatment"]

    merged.background = "\n\n".join(backgrounds)[:_BACKGROUND_LIMIT]
    return merged


# ── paragraph extraction (issue summarisation input) ─────────────────

# Judgments number paragraphs at line starts as "1  The plaintiff ..." or
# "[1] The plaintiff ..." (sometimes "1." or "1)").
_PARA_LINE = re.compile(r"^\s{0,4}\[?(\d{1,4})\]?(?:[ \t.)]|$)", re.MULTILINE)
_TAIL_PARAS = 2  # slice generously: include a couple of paragraphs past the last ref


def _para_numbers(para_refs: list[str]) -> set[int]:
    nums: set[int] = set()
    for ref in para_refs:
        found = [int(n) for n in re.findall(r"\d+", ref)]
        if not found:
            continue
        if "-" in ref and len(found) >= 2 and found[0] <= found[1] <= found[0] + 500:
            nums.update(range(found[0], found[1] + 1))
        else:
            nums.update(found)
    return nums


def _para_positions(raw_text: str) -> dict[int, int]:
    positions: dict[int, int] = {}
    for match in _PARA_LINE.finditer(raw_text):
        num = int(match.group(1))
        if num not in positions:
            positions[num] = match.start()
    return positions


def extract_relevant_paragraphs(
    raw_text: str, para_refs: list[str], question: str = ""
) -> str:
    """Slice the judgment text around an issue's paragraph references.

    Slices from the first referenced paragraph to two paragraphs past the
    last. If the refs are missing or unmatchable, falls back to the full
    text when it fits 20k tokens, else to the chunk best matching the
    issue question's keywords.
    """
    nums = _para_numbers(para_refs)
    if nums:
        positions = _para_positions(raw_text)
        start_pos = positions.get(min(nums))
        if start_pos is not None:
            last = max(nums) + _TAIL_PARAS
            ends = [p for n, p in positions.items() if n > last and p > start_pos]
            end_pos = min(ends) if ends else len(raw_text)
            return raw_text[start_pos:end_pos]

    if _estimate_tokens(raw_text) <= MEDIUM_TOKEN_LIMIT:
        return raw_text
    tokens = _question_tokens(question)
    chunks = split_with_overlap(raw_text)
    return max(chunks, key=lambda c: len(tokens & _question_tokens(c)))


# ── prompt assembly ──────────────────────────────────────────────────


def _hint_block(raw_item: RawItem) -> str:
    """Seed advantage: Zeeker's court_summary/subject_tags as marked hints."""
    hints = []
    summary = raw_item.extras.get("court_summary", "").strip()
    tags = raw_item.extras.get("subject_tags", "").strip()
    if summary:
        hints.append(f"Court summary: {summary}")
    if tags:
        hints.append(f"Subject tags: {tags}")
    if not hints:
        return ""
    joined = "\n".join(hints)
    return (
        "CATALOGUE HINTS — these come from the data catalogue, not from the "
        "judgment text below. Treat them as hints only, NOT ground truth: "
        "verify every detail against the judgment text and extract nothing "
        "from the hints alone.\n"
        f"{joined}\n\n"
        "JUDGMENT TEXT:\n"
    )


def _issue_user_prompt(question: str, relevant_text: str) -> str:
    return f"Issue: {question}\n\nRelevant extract of the decision:\n{relevant_text}"


def _digest(structure: Structure) -> str:
    """Serialise the structured data for the cookie-generation call."""
    return json.dumps(
        {
            "citation": structure.citation,
            "court": structure.court_name,
            "parties": structure.parties,
            "background": structure.background,
            "issues": [
                {
                    "question": issue.question,
                    "holding": issue.holding,
                    "reasoning": issue.reasoning,
                }
                for issue in structure.issues
            ],
            "legislation": structure.legislation,
            "cases_cited": structure.cases_cited,
            "orders": structure.orders,
        },
        indent=2,
        ensure_ascii=False,
    )


# ── meta assembly and result type ────────────────────────────────────


def _placeholder(label: str) -> FolioRef:
    # Interface contract with folio.resolve_judgment_meta: raw free-text
    # labels arrive as unresolved placeholder refs.
    return FolioRef(iri=None, preferred_label=label, branch=_UNRESOLVED_BRANCH, confidence=0.0)


def _build_meta(source_id: str, structure: Structure) -> JudgmentMeta:
    return JudgmentMeta(
        source_id=source_id,
        citation=structure.citation,
        court=_placeholder(structure.court_name) if structure.court_name else None,
        judges=structure.judges,
        parties=structure.parties,
        issues=[
            JudgmentIssue(
                question=issue.question,
                holding=issue.holding,
                reasoning=issue.reasoning,
                folio_concepts=[_placeholder(c) for c in issue.raw_concepts],
            )
            for issue in structure.issues
        ],
        legislation=[_placeholder(name) for name in structure.legislation],
        cases_cited=[
            CaseCitation(citation=case["citation"], treatment=case["treatment"])
            for case in structure.cases_cited
        ],
        orders=structure.orders,
    )


def _meta_unresolved(meta: JudgmentMeta) -> list[str]:
    """Labels that stayed unresolved after FOLIO resolution, for review."""
    refs: list[FolioRef] = []
    if meta.court is not None:
        refs.append(meta.court)
    refs.extend(meta.legislation)
    for issue in meta.issues:
        refs.extend(issue.folio_concepts)
    out: list[str] = []
    for ref in refs:
        if ref.branch == _UNRESOLVED_BRANCH and ref.preferred_label not in out:
            out.append(ref.preferred_label)
    return out


class JudgmentCookies(list):
    """list[Cookie] return value carrying the JudgmentMeta and any warnings."""

    def __init__(
        self,
        cookies: list[Cookie] = (),
        meta: JudgmentMeta | None = None,
        warnings: list[str] = (),
    ):
        super().__init__(cookies)
        self.meta = meta
        self.warnings = list(warnings)


def _cookies_for_source(conn: sqlite3.Connection, source_id: str) -> list[Cookie]:
    rows = conn.execute(
        "SELECT cookie_id FROM cookie_sources WHERE source_id = ?", (source_id,)
    ).fetchall()
    cookies = (db.get_cookie(conn, row["cookie_id"]) for row in rows)
    return [cookie for cookie in cookies if cookie is not None]


# ── the pipeline ─────────────────────────────────────────────────────


def process_judgment(
    raw_item: RawItem,
    conn: sqlite3.Connection,
    llm: object,
    folio_client: httpx.Client,
    model: str = DEFAULT_MODEL,
) -> JudgmentCookies:
    """Triage -> structure -> issue summaries -> cookies -> FOLIO -> store.

    `llm` is an LLMBackend (Anthropic or Ollama) or a bare Anthropic client.
    Returns the cookies linked to this judgment (a list[Cookie] subclass
    with `.meta` and `.warnings`). If the source URL was already ingested,
    returns the existing cookies and stored meta without any LLM call.
    """
    existing = db.find_source_by_url(conn, raw_item.source_url)
    if existing is not None:
        return JudgmentCookies(
            _cookies_for_source(conn, existing.id),
            meta=db.get_judgment_meta(conn, existing.id),
        )

    backend = as_backend(llm, model)
    warnings: list[str] = []
    token_count = _estimate_tokens(raw_item.raw_text)
    path = triage(token_count)
    num_ctx = CHUNK_NUM_CTX if path != "short" else None
    hints = _hint_block(raw_item)

    # ── Step 2: structural extraction ────────────────────────────────
    if path == "short":
        structure = _parse_structure(
            backend.structured(
                system=JUDGMENT_SINGLE_PASS,
                user=hints + raw_item.raw_text,
                schema=JUDGMENT_SINGLE_PASS_TOOL["input_schema"],
                tool_name=JUDGMENT_SINGLE_PASS_TOOL["name"],
            )
        )
    elif path == "medium":
        structure = _parse_structure(
            backend.structured(
                system=JUDGMENT_STRUCTURE,
                user=hints + raw_item.raw_text,
                schema=JUDGMENT_STRUCTURE_TOOL["input_schema"],
                tool_name=JUDGMENT_STRUCTURE_TOOL["name"],
                num_ctx=num_ctx,
            )
        )
    else:
        chunks = split_with_overlap(raw_item.raw_text)
        partials = []
        for index, chunk in enumerate(chunks):
            partials.append(
                _parse_structure(
                    backend.structured(
                        system=JUDGMENT_CHUNK_STRUCTURE,
                        user=(hints + chunk) if index == 0 else chunk,
                        schema=JUDGMENT_CHUNK_TOOL["input_schema"],
                        tool_name=JUDGMENT_CHUNK_TOOL["name"],
                        num_ctx=num_ctx,
                    )
                )
            )
        structure = merge_structures(partials)

    # Catalogue fields beat LLM extraction where Zeeker provides them:
    # the smoke test stored "OC 1154/2025" (a case number the model chose)
    # while extras carried the authoritative "[2026] SGDC 136".
    if raw_item.extras.get("citation", "").strip():
        structure.citation = raw_item.extras["citation"].strip()
    if raw_item.extras.get("court", "").strip():
        structure.court_name = raw_item.extras["court"].strip()

    # ── Step 3: issue-level summarisation (medium/long) ──────────────
    if path != "short":
        if len(structure.issues) > MAX_ISSUES_SUMMARISED:
            message = (
                f"{len(structure.issues)} issues found; summarising only the "
                f"first {MAX_ISSUES_SUMMARISED} (cost cap)"
            )
            warnings.append(message)
            logger.warning("%s: %s", raw_item.source_url, message)
        for issue in structure.issues[:MAX_ISSUES_SUMMARISED]:
            relevant = extract_relevant_paragraphs(
                raw_item.raw_text, issue.para_refs, question=issue.question
            )
            summary = backend.structured(
                system=JUDGMENT_ISSUE_SUMMARY,
                user=_issue_user_prompt(issue.question, relevant),
                schema=JUDGMENT_ISSUE_SUMMARY_TOOL["input_schema"],
                tool_name=JUDGMENT_ISSUE_SUMMARY_TOOL["name"],
                num_ctx=num_ctx,
            )
            issue.holding = str(summary.get("holding", ""))
            issue.reasoning = str(summary.get("reasoning", ""))
            issue.raw_concepts = [str(c) for c in summary.get("raw_concepts") or []]

    # ── Step 4: cookie generation from the structured digest ─────────
    cookie_data = backend.structured(
        system=JUDGMENT_COOKIE_PROMPT,
        user=_digest(structure),
        schema=NEWS_EXTRACTION_TOOL["input_schema"],
        tool_name=NEWS_EXTRACTION_TOOL["name"],
    )
    topics = [
        TopicExtraction.model_validate(topic)
        for topic in cookie_data.get("topics") or []
    ]

    # ── Step 5: FOLIO resolution + internal citation cross-linking ───
    for topic in topics:
        resolve_topic(topic, folio_client)

    source = Source(
        source_url=raw_item.source_url,
        zeeker_url=raw_item.zeeker_url,
        title=raw_item.title,
        raw_text=raw_item.raw_text,
        date=raw_item.date,
        source_id=raw_item.source_id,
        item_type=raw_item.item_type,
        license=raw_item.license,
        token_count=token_count,
    )
    meta = _build_meta(source.id, structure)
    resolve_judgment_meta(folio_client, meta)

    known_citations = db.list_judgment_citations(conn)
    for case in meta.cases_cited:
        case.internal_ref = known_citations.get(" ".join(case.citation.split()).lower())

    # ── Step 6: store ─────────────────────────────────────────────────
    db.upsert_source(conn, source)
    db.save_judgment_meta(conn, source.id, meta)

    cookies: list[Cookie] = []
    unresolved: list[str] = []
    for topic in topics:
        cookie = Cookie(
            source_ids=[source.id],
            headline=topic.headline,
            summary=topic.summary,
            why_it_matters=topic.why_it_matters,
            significance=topic.significance,
            folio_areas=topic.folio_areas,
            folio_entities=topic.folio_entities,
            folio_concepts=topic.folio_concepts,
            unresolved=topic.unresolved,
        )
        db.save_cookie(conn, cookie)
        unresolved.extend(term for term in topic.unresolved if term not in unresolved)
        cookies.append(cookie)

    unresolved.extend(t for t in _meta_unresolved(meta) if t not in unresolved)
    if unresolved:
        db.record_unresolved_terms(conn, unresolved)

    return JudgmentCookies(cookies, meta=meta, warnings=warnings)
