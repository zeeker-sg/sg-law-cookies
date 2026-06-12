"""Prompts and tool schemas for the judgment pipeline (PRD section 4.2,
enrichment pseudocode section 3).

Four extraction prompts (single-pass, structure, chunk-structure, issue
summary) each paired with a tool schema, plus the cookie-generation prompt
which reuses the news pipeline's NEWS_EXTRACTION_TOOL so both pipelines are
interchangeable downstream.

All prompts must tolerate non-court decisions (e.g. PDPC enforcement
decisions): "citation" may be a case/decision number, "court_name" the
regulator, "parties" the respondent(s), and "orders" the penalty or
directions imposed.
"""

from typing import Any

from sg_law_cookies.extraction import NEWS_EXTRACTION_TOOL

__all__ = [
    "JUDGMENT_SINGLE_PASS",
    "JUDGMENT_SINGLE_PASS_TOOL",
    "JUDGMENT_STRUCTURE",
    "JUDGMENT_STRUCTURE_TOOL",
    "JUDGMENT_CHUNK_STRUCTURE",
    "JUDGMENT_CHUNK_TOOL",
    "JUDGMENT_ISSUE_SUMMARY",
    "JUDGMENT_ISSUE_SUMMARY_TOOL",
    "JUDGMENT_COOKIE_PROMPT",
    "JUDGMENT_COOKIE_TOOL",
]

# ──────────────────────────────────────────────────────────────────────
# Shared schema fragments
# ──────────────────────────────────────────────────────────────────────

_CASE_CITED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "citation": {
            "type": "string",
            "description": "The cited case's citation exactly as it appears in the text.",
        },
        "treatment": {
            "type": "string",
            "enum": ["followed", "distinguished", "overruled", "referred"],
            "description": (
                "How this decision treated the cited case. Use 'referred' "
                "when the treatment is not explicitly stated."
            ),
        },
    },
    "required": ["citation", "treatment"],
    "additionalProperties": False,
}

_PARA_REFS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Paragraph references where this issue is discussed, exactly as "
        "numbered in the text (e.g. '12', '45-78'). Empty if the text has "
        "no paragraph numbering."
    ),
}

_ANTI_HALLUCINATION = """\
Hard rules (apply to every field):
- Extract only what is stated in the text you are given. Never guess.
- Do not invent citations, case names, paragraph numbers, statutes, party
  names, judge names, figures or dates. If a detail is not in the text,
  use an empty string or empty list for that field.
- Copy citations and paragraph numbers exactly as they appear; do not
  normalise, complete or correct them.
- Do not import knowledge about this case, the parties or the law from
  outside the text.
"""

_NON_COURT_NOTE = """\
The document may not be a court judgment. It may be a regulator's or
tribunal's decision (e.g. a PDPC enforcement decision). In that case:
- citation: the decision or case number used by the regulator/tribunal.
- court_name: the regulator or tribunal (e.g. "Personal Data Protection
  Commission").
- judges: the deciding officer(s) or commissioner(s), if named.
- parties: the organisation(s) involved, e.g. the respondent to an
  enforcement action.
- orders: the outcome — the breach found, financial penalty, directions,
  warning, or no further action.
"""

# ──────────────────────────────────────────────────────────────────────
# 1. Single-pass extraction (short judgments, < 5,000 tokens)
# ──────────────────────────────────────────────────────────────────────

JUDGMENT_SINGLE_PASS = f"""\
You are a legal analyst for SG Law Cookies, a daily digest of Singapore
legal developments. You will be given the full text of one short Singapore
judgment or regulatory decision. In a single pass, extract its complete
structure AND summarise each legal issue.

Extract:
- citation: the decision's own neutral citation or decision number
  (e.g. "[2026] SGHC 34", or a regulator's case number).
- court_name: the court, tribunal or regulator that decided the matter.
- judges: the judge(s), coram, or deciding officer(s), as named.
- parties: the parties (or respondent, for enforcement decisions).
- background: the factual background, compressed into a few sentences —
  what happened and how the dispute arose.
- issues: each distinct legal issue the decision-maker actually decided.
  For each issue:
  - question: the issue framed as a neutral question of law or mixed
    law and fact.
  - holding: what was decided on that issue, in one sentence.
  - reasoning: the core reasoning in 2-3 sentences — why the
    decision-maker reached that holding.
  - raw_concepts: free-text legal doctrines, principles or concepts
    engaged by this issue (e.g. "minimum legal standard of protection",
    "unfair preference"). A later step normalises these against the
    FOLIO ontology.
  - para_refs: paragraph references where the issue is discussed, if the
    text is paragraph-numbered.
- legislation: statutes, subsidiary legislation and specific provisions
  considered, as named in the text.
- cases_cited: every case the decision cites, with its treatment
  (followed / distinguished / overruled / referred). Use 'referred' when
  the treatment is not explicit.
- orders: the orders made or outcome imposed, in one or two sentences.

{_NON_COURT_NOTE}
{_ANTI_HALLUCINATION}\
- Summaries (background, holding, reasoning) must be your own words, but
  every claim in them must be grounded in the text.
"""

JUDGMENT_SINGLE_PASS_TOOL: dict[str, Any] = {
    "name": "record_judgment",
    "description": (
        "Record the full structured extraction of a short judgment or "
        "regulatory decision: skeleton plus per-issue summaries."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "citation": {
                "type": "string",
                "description": "Neutral citation or decision/case number, verbatim.",
            },
            "court_name": {
                "type": "string",
                "description": "Court, tribunal or regulator that decided the matter.",
            },
            "judges": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Judge(s) or deciding officer(s), as named in the text.",
            },
            "parties": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Parties (or respondent, for enforcement decisions).",
            },
            "background": {
                "type": "string",
                "description": "Compressed factual background, a few sentences.",
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The legal issue framed as a question.",
                        },
                        "holding": {
                            "type": "string",
                            "description": "What was decided on this issue, one sentence.",
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "Core reasoning, 2-3 sentences.",
                        },
                        "raw_concepts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Free-text legal doctrines/concepts engaged.",
                        },
                        "para_refs": _PARA_REFS_SCHEMA,
                    },
                    "required": ["question", "holding", "reasoning", "raw_concepts"],
                    "additionalProperties": False,
                },
            },
            "legislation": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Legislation and provisions considered, as named.",
            },
            "cases_cited": {
                "type": "array",
                "items": _CASE_CITED_SCHEMA,
                "description": "Cases cited and their treatment.",
            },
            "orders": {
                "type": "string",
                "description": "Orders made or outcome imposed.",
            },
        },
        "required": [
            "citation",
            "court_name",
            "judges",
            "parties",
            "background",
            "issues",
            "legislation",
            "cases_cited",
            "orders",
        ],
        "additionalProperties": False,
    },
}

# ──────────────────────────────────────────────────────────────────────
# 2. Structure-only extraction (medium judgments, first pass of two)
# ──────────────────────────────────────────────────────────────────────

JUDGMENT_STRUCTURE = f"""\
You are a legal analyst for SG Law Cookies. You will be given the full text
of one Singapore judgment or regulatory decision. Extract its SKELETON only.

This is a CLASSIFICATION task, not a summarisation task. You are answering
"what are the parts of this decision", not "what does it mean". Do NOT
write holdings, reasoning or analysis — a later step summarises each issue
separately using the paragraph references you provide here.

Extract:
- citation: the decision's own neutral citation or decision number.
- court_name: the court, tribunal or regulator.
- judges: the judge(s) or deciding officer(s), as named.
- parties: the parties (or respondent, for enforcement decisions).
- background: the factual background, compressed into a few sentences.
- issues: each distinct legal issue actually decided. For each issue give
  ONLY:
  - question: the issue framed as a neutral question.
  - para_refs: the paragraph references where that issue is discussed.
    These references are essential — they drive the later per-issue
    summarisation step. Copy paragraph numbers exactly as they appear;
    ranges like "45-78" are fine. Leave empty only if the text has no
    paragraph numbering.
- legislation: statutes and provisions considered, as named in the text.
- cases_cited: every case cited, with its treatment (followed /
  distinguished / overruled / referred; 'referred' when not explicit).
- orders: the orders made or outcome imposed.

{_NON_COURT_NOTE}
{_ANTI_HALLUCINATION}"""

JUDGMENT_STRUCTURE_TOOL: dict[str, Any] = {
    "name": "record_structure",
    "description": (
        "Record the skeleton of a judgment or regulatory decision: parties, "
        "issues as questions with paragraph references, citations. No "
        "holdings or reasoning."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "citation": {
                "type": "string",
                "description": "Neutral citation or decision/case number, verbatim.",
            },
            "court_name": {
                "type": "string",
                "description": "Court, tribunal or regulator that decided the matter.",
            },
            "judges": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Judge(s) or deciding officer(s), as named in the text.",
            },
            "parties": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Parties (or respondent, for enforcement decisions).",
            },
            "background": {
                "type": "string",
                "description": "Compressed factual background, a few sentences.",
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The legal issue framed as a question.",
                        },
                        "para_refs": _PARA_REFS_SCHEMA,
                    },
                    "required": ["question", "para_refs"],
                    "additionalProperties": False,
                },
            },
            "legislation": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Legislation and provisions considered, as named.",
            },
            "cases_cited": {
                "type": "array",
                "items": _CASE_CITED_SCHEMA,
                "description": "Cases cited and their treatment.",
            },
            "orders": {
                "type": "string",
                "description": "Orders made or outcome imposed.",
            },
        },
        "required": [
            "citation",
            "court_name",
            "judges",
            "parties",
            "background",
            "issues",
            "legislation",
            "cases_cited",
            "orders",
        ],
        "additionalProperties": False,
    },
}

# ──────────────────────────────────────────────────────────────────────
# 3. Chunk structure extraction (long judgments, per-chunk pass)
# ──────────────────────────────────────────────────────────────────────

JUDGMENT_CHUNK_STRUCTURE = f"""\
You are a legal analyst for SG Law Cookies. You will be given ONE CHUNK of
a longer Singapore judgment or regulatory decision — a contiguous slice,
not the whole document. Other chunks are processed separately and the
partial extractions are merged afterwards.

Extract the SKELETON of THIS CHUNK ONLY. This is a classification task,
not a summarisation task: no holdings, no reasoning, no analysis.

Because this is a partial document, most fields may legitimately be
absent from your chunk:
- The citation, court, judges and parties usually appear only in the
  opening chunk. The orders usually appear only in the final chunk.
- If a field's information is not in this chunk, return an empty string
  or empty list for it. NEVER guess or reconstruct what is not in this
  chunk — the merge step relies on absent fields being empty.
- An issue may begin in one chunk and continue in another. Record only
  what this chunk shows: the question as framed (or as best discernible
  from this chunk) and the paragraph references visible in this chunk.

Extract (each only insofar as it appears in this chunk):
- citation, court_name, judges, parties
- background: factual background appearing in this chunk, compressed.
- issues: legal issues discussed in this chunk, each as a question with
  the paragraph references visible in this chunk.
- legislation: statutes and provisions named in this chunk.
- cases_cited: cases cited in this chunk, with treatment ('referred'
  when not explicit).
- orders: orders or outcome, only if stated in this chunk.

{_NON_COURT_NOTE}
{_ANTI_HALLUCINATION}"""

JUDGMENT_CHUNK_TOOL: dict[str, Any] = {
    "name": "record_chunk_structure",
    "description": (
        "Record the partial skeleton found in one chunk of a longer "
        "judgment. Fields not present in the chunk are empty."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "citation": {
                "type": "string",
                "description": "Citation if it appears in this chunk, else empty string.",
            },
            "court_name": {
                "type": "string",
                "description": "Court/tribunal/regulator if named in this chunk, else empty.",
            },
            "judges": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Judges/deciding officers named in this chunk, possibly empty.",
            },
            "parties": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Parties named in this chunk, possibly empty.",
            },
            "background": {
                "type": "string",
                "description": "Factual background appearing in this chunk, else empty.",
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Issue discussed in this chunk, as a question.",
                        },
                        "para_refs": _PARA_REFS_SCHEMA,
                    },
                    "required": ["question", "para_refs"],
                    "additionalProperties": False,
                },
            },
            "legislation": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Legislation named in this chunk, possibly empty.",
            },
            "cases_cited": {
                "type": "array",
                "items": _CASE_CITED_SCHEMA,
                "description": "Cases cited in this chunk, possibly empty.",
            },
            "orders": {
                "type": "string",
                "description": "Orders/outcome only if stated in this chunk, else empty.",
            },
        },
        "required": [
            "citation",
            "court_name",
            "judges",
            "parties",
            "background",
            "issues",
            "legislation",
            "cases_cited",
            "orders",
        ],
        "additionalProperties": False,
    },
}

# ──────────────────────────────────────────────────────────────────────
# 4. Per-issue summarisation (medium/long judgments, second pass)
# ──────────────────────────────────────────────────────────────────────

JUDGMENT_ISSUE_SUMMARY = f"""\
You are a legal analyst for SG Law Cookies. You will be given ONE legal
issue (framed as a question) from a Singapore judgment or regulatory
decision, together with the portion of the decision's text relevant to
that issue.

Answer for THIS ISSUE ONLY — ignore other issues that may be visible in
the extract:
- holding: what the decision-maker decided on this issue, in one sentence.
- reasoning: the core reasoning in 2-3 sentences — why they decided it
  that way, including any test applied or factor that was decisive.
- raw_concepts: free-text legal doctrines, principles or concepts engaged
  by this issue (e.g. "duty of care", "purposive interpretation"). A later
  step normalises these against the FOLIO ontology.

The decision-maker may be a court, tribunal or regulator (e.g. the PDPC
in an enforcement decision); write the holding accordingly (e.g. a breach
finding and penalty rather than a judgment between parties).

{_ANTI_HALLUCINATION}\
- Write the holding and reasoning in your own words, but every claim must
  be grounded in the supplied extract. If the extract does not show how
  the issue was resolved, say so in the holding rather than inventing an
  outcome.
"""

JUDGMENT_ISSUE_SUMMARY_TOOL: dict[str, Any] = {
    "name": "record_issue_summary",
    "description": "Record the holding, reasoning and concepts for one legal issue.",
    "input_schema": {
        "type": "object",
        "properties": {
            "holding": {
                "type": "string",
                "description": "What was decided on this issue, one sentence.",
            },
            "reasoning": {
                "type": "string",
                "description": "Core reasoning, 2-3 sentences.",
            },
            "raw_concepts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Free-text legal doctrines/concepts engaged by this issue.",
            },
        },
        "required": ["holding", "reasoning", "raw_concepts"],
        "additionalProperties": False,
    },
}

# ──────────────────────────────────────────────────────────────────────
# 5. Cookie generation (structured digest -> news-shaped topics)
# ──────────────────────────────────────────────────────────────────────

# Intentionally the same schema as the news pipeline (pseudocode section 3,
# step 4) so judgment cookies are interchangeable downstream.
JUDGMENT_COOKIE_TOOL: dict[str, Any] = NEWS_EXTRACTION_TOOL

JUDGMENT_COOKIE_PROMPT = """\
You are a legal analyst for SG Law Cookies, a daily digest of Singapore
legal developments. Your reader is a Singapore-qualified lawyer who is
pressed for time: they need the signal, not a case digest.

You will be given the STRUCTURED extraction of one Singapore judgment or
regulatory decision (citation, court or regulator, parties, background,
issues with holdings and reasoning, legislation, cases cited, orders) —
not the raw text. Produce cookies from it with the extraction tool.

Splitting rules:
- Produce one topic per issue with INDEPENDENT SIGNAL VALUE — an issue a
  practitioner might separately need to act on, be aware of, or track
  (e.g. a clarified legal test, a new application of a statute, notable
  treatment of an earlier authority).
- Routine or wholly fact-specific issues with no signal beyond the case's
  outcome do not get their own topic; fold them into a single topic for
  the decision as a whole if the outcome itself is worth reporting.
- The number of topics follows the number of issues with independent
  signal value, never the length of the decision.

For each topic:
- headline: one crisp sentence stating the legal development, naturally
  citing the case (e.g. "High Court holds in Tan v Lim [2026] SGHC 34
  that ..."; for regulators, e.g. "PDPC fines ... for ..."). Use the
  citation exactly as given in the structured data.
- summary: 2-3 sentences — the question, the holding, and the core
  reasoning or test applied.
- why_it_matters: exactly one sentence on the practical implication for
  a Singapore lawyer.
- raw_areas: free-text areas of law engaged (e.g. "data protection",
  "company law"). A later step normalises these against FOLIO.
- raw_entities: free-text names of the court or regulator, parties,
  statutes and other entities involved.
- raw_concepts: free-text legal doctrines, principles or concepts engaged.

Significance rubric — assign exactly one level per topic:
- high: creates new legal obligations, changes existing law, or introduces
  a new statutory framework (e.g. a new or reformulated legal test, an
  earlier authority overruled).
- medium: updates or clarifies existing rules, adjusts thresholds, or
  extends existing schemes (e.g. an established test applied to a new
  situation, guidance on quantum or penalties).
- low: commentary, market data, general interest, or routine proceedings
  (e.g. an orthodox application of settled law).

The decision may come from a regulator or tribunal rather than a court
(e.g. a PDPC enforcement decision against a respondent, finding a breach
and imposing a financial penalty). Frame such topics around the breach
found, the penalty, and what conduct the regulator faulted.

Hard rules:
- Use only the structured data you are given. Do not invent facts,
  figures, parties, citations or outcomes that are not in it.
- Do not embellish holdings or reasoning beyond what the structured data
  states.
- If the structured data contains no issue with any signal value, return
  an empty topics list rather than inventing one.
"""
