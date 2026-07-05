"""Argus analyst — EDGAR filings text layer (analyst-module §1 Stages 1/4/5/6, §3).

Fetches the latest 10-K (Risk Factors, Item 1A; MD&A, Item 7) and DEF 14A proxy
(beneficial-ownership and executive-compensation blocks) AT RUN TIME and freezes
section-bounded RAW TEXT into the data pack. Filings text is never stored in
tables — the pack is its storage (§3), and the dossier quotes retrieved text only
(Law 2).

Section bounding is deliberately boring (Law 8): stdlib-only HTML stripping, then
case-insensitive heading regexes over the flattened text.

- 10-K items use the LONGEST-SPAN rule: every "Item 1A" occurrence is a candidate
  start, every "Item 1B"/"Item 2" occurrence a candidate end, and the widest
  (start → next end) span wins. Tables of contents lose automatically (their spans
  are one line); in-section cross-references lose because the true heading precedes
  them. No DOM parsing.
- DEF 14A has no item structure; proxy blocks use the LAST heading occurrence
  (a TOC entry precedes the body heading) and a fixed char budget forward.

A section that cannot be bounded — or bounds to under _MIN_SECTION_CHARS — is
None, and the pack says "not available" (Law 2: never a guessed excerpt). Budgets
cap each section; truncation is explicit ({"truncated": true, "chars_original"}).

Run:  python -m analyst.filings TSLA   (prints section names, sizes, first 200 chars)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import html as html_lib
import re

from ingestion.sec_facts import USER_AGENT
from shared.exceptions import FetchError
from shared.fetcher_base import fetch_with_retry

SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL_TEMPLATE = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/{doc}"

# Per-section char budgets for the frozen pack. Sized so the four sections total
# ~200 KB — large enough that nothing analytically load-bearing is cut for a
# typical filer, small enough to keep pack storage and synthesis context sane.
SECTION_BUDGETS = {
    "risk_factors": 60_000,
    "mdna": 80_000,
    "ownership": 25_000,
    "compensation": 50_000,
}

# Below this, a "section" is a TOC line or heading fragment, not the section.
_MIN_SECTION_CHARS = 500

# DEF 14A block headings, each a PRIORITY tuple: the first pattern that bounds a
# real block (>= _MIN_SECTION_CHARS) wins, later ones are fallbacks. Covers the
# standard proxy phrasing plus the variants large filers actually use (TSLA titles
# the beneficial-ownership table plain "Ownership of Securities" — probed on the
# 2025-09-17 proxy). For compensation, CD&A is preferred over the Summary
# Compensation Table so the block anchors at the discussion, not a later table.
_PROXY_HEADINGS: dict[str, tuple[str, ...]] = {
    "ownership": (
        r"security\s+ownership\s+of\s+certain\s+beneficial\s+owners",
        r"ownership\s+of\s+securities",
        r"beneficial\s+ownership\s+of\s+(common\s+stock|securities)",
    ),
    "compensation": (
        r"compensation\s+discussion\s+and\s+analysis",
        r"summary\s+compensation\s+table",
    ),
}


def html_to_text(html: str) -> str:
    """Flatten filing HTML to searchable text (stdlib only; Law 8).

    Scripts/styles/comments drop; block-level tags become newlines so headings
    stay line-separated; entities decode; NBSPs normalize to spaces (EDGAR
    headings are full of them); whitespace collapses.
    """
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)</?(p|div|br|tr|table|h[1-6]|li|ul|ol)[^>]*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    # NBSP family and Unicode spaces (EDGAR headings are full of them) -> plain space.
    text = re.sub(r"[  -​ 　]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*(\n[ \t]*)+", "\n\n", text)
    return text.strip()


def _item_pattern(item: str) -> re.Pattern:
    """A heading pattern for a 10-K item number ('1A', '7', ...).

    LINE-ANCHORED (^ with MULTILINE): after html_to_text, real headings and TOC
    entries sit at line starts, while in-prose cross-references ('the risks
    described in Item 1A of ...') are mid-sentence and must not match — an early
    cross-reference would otherwise win the longest span and drag Item 1 in with
    it (observed on TSLA's FY2025 10-K forward-looking-statements paragraph).
    TOC entries DO match and lose on span length (a TOC line ends at the next TOC
    line). ``\\b`` after the item number keeps 'Item 7' from matching 'Item 7A'
    (digit→letter is not a word boundary) and 'Item 1' from 'Item 1A'.
    """
    return re.compile(rf"(?im)^[ >•\-]*item\s*{re.escape(item)}\b")


# Any line-anchored item heading — the TOC-signature probe (see extract_item_section).
_ANY_ITEM_PATTERN = re.compile(r"(?im)^[ >•\-]*item\s*\d+[a-z]?\b")

# A start followed by ANOTHER item heading within this window is a TOC entry
# (TOC lines are consecutive); a real heading is followed by prose.
_TOC_PROXIMITY_CHARS = 200


def _is_toc_line(text: str, start: int) -> bool:
    """True when the line at ``start`` reads as a TOC entry, not a section heading.

    Two independent signatures, either sufficing: another item heading within
    _TOC_PROXIMITY_CHARS (TOC entries are consecutive — catches hyperlinked TOCs
    with no visible page numbers), or the line ending in digits (a page number —
    catches a TOC entry that is last in its TOC). Real headings have neither.
    """
    if _ANY_ITEM_PATTERN.search(text, start + 1, min(len(text), start + _TOC_PROXIMITY_CHARS)):
        return True
    eol = text.find("\n", start)
    line = text[start : eol if eol != -1 else len(text)]
    return bool(re.search(r"\d\s*$", line))


def extract_item_section(text: str, start_item: str, end_items: tuple[str, ...]) -> str | None:
    """The item's section text, or None. Three stacked rules (module docstring):

    1. Line anchor (in ``_item_pattern``) kills in-prose cross-references.
    2. TOC filter (``_is_toc_line``): consecutive-item proximity and page-number
       line endings each mark a start as a TOC entry — dropped. Without this, a
       TOC entry can hand the longest span everything between itself and the real
       section. If every start looks like TOC, fall back to all of them
       (min-section-chars still guards downstream).
    3. Longest span wins among survivors — running page-headers repeat INSIDE the
       section, so the true heading (earliest survivor) has the widest span.
    """
    starts = [m.start() for m in _item_pattern(start_item).finditer(text)]
    if not starts:
        return None
    non_toc = [s for s in starts if not _is_toc_line(text, s)]
    starts = non_toc or starts
    ends = sorted(
        m.start() for item in end_items for m in _item_pattern(item).finditer(text)
    )
    best_start, best_span = None, -1
    for s in starts:
        e = next((x for x in ends if x > s), len(text))
        if e - s > best_span:
            best_start, best_span = s, e - s
    if best_start is None:
        return None
    return text[best_start : best_start + best_span]


def extract_proxy_block(text: str, heading_patterns: tuple[str, ...]) -> str | None:
    """All text from a proxy heading to the document end, or None.

    Patterns are tried in priority order; within a pattern the LAST occurrence
    wins (a TOC entry precedes the body heading). A hit too close to the document
    end to bound a real block falls through to the next pattern.

    NO clipping here — the caller's ``_section_payload`` applies the budget so its
    truncation metadata stays truthful (clipping in two places once mislabeled a
    clipped proxy block as untruncated). A proxy has no reliable end-heading, so
    "the section" runs to the document end and ``chars_original`` reads as "chars
    available from the heading", with ``truncated`` meaning "less was kept".
    """
    for pattern in heading_patterns:
        # Line-anchored like _item_pattern: real headings sit at line starts after
        # html_to_text; in-prose references (footnotes, cross-refs) are mid-sentence.
        matches = list(re.finditer(rf"(?im)^[ >•\-]*{pattern}", text))
        if not matches:
            continue
        block = text[matches[-1].start() :]
        if len(block.strip()) >= _MIN_SECTION_CHARS:
            return block
    return None


def _section_payload(raw: str | None, budget: int) -> dict | None:
    """Clip a raw section to its budget with explicit truncation metadata.

    None in → None out (the pack renders "not available"); under
    _MIN_SECTION_CHARS is treated as not-found (a heading fragment, not a section).
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if len(stripped) < _MIN_SECTION_CHARS:
        return None
    return {
        "text": stripped[:budget],
        "chars_original": len(stripped),
        "truncated": len(stripped) > budget,
    }


def latest_filing_meta(cik10: str, form: str, run_id: str) -> dict | None:
    """Metadata for the most recent filing of ``form``, from the submissions index.

    Returns {form, accn, filed, report_date, primary_document, url} or None when
    the issuer has never filed that form (a real, renderable absence — Law 2).
    """
    doc = fetch_with_retry(
        SUBMISSIONS_URL_TEMPLATE.format(cik=cik10),
        {"User-Agent": USER_AGENT},
        {},
        "analyst:filings_index",
        run_id,
    )
    recent = doc.get("filings", {}).get("recent", {})
    rows = zip(
        recent.get("form", []),
        recent.get("accessionNumber", []),
        recent.get("filingDate", []),
        recent.get("reportDate", []),
        recent.get("primaryDocument", []),
    )
    for f, accn, filed, report_date, primary in rows:
        if f == form and primary:
            return {
                "form": form,
                "accn": accn,
                "filed": filed,
                "report_date": report_date or None,
                "primary_document": primary,
                "url": ARCHIVE_URL_TEMPLATE.format(
                    cik_int=int(cik10), accn_nodash=accn.replace("-", ""), doc=primary
                ),
            }
    return None


def _fetch_filing_text(url: str, run_id: str) -> str:
    """The filing's primary document, flattened to text (wrapped fetch, §12)."""
    html = fetch_with_retry(
        url, {"User-Agent": USER_AGENT}, {}, "analyst:filings_doc", run_id, parse="text"
    )
    return html_to_text(html)


def filings_block(symbol: str, cik10: str | None, run_id: str) -> dict:
    """The pack's filings sub-document: latest 10-K + DEF 14A sections for one issuer.

    Every failure mode stays visible instead of aborting the pack (Law 7): an
    unresolvable CIK, a never-filed form, an unreachable document (already in
    fetch_log via the wrapped fetcher), and an unboundable section each produce an
    explicit marker at their level.
    """
    if not cik10:
        return {"note": "CIK unresolved: no EDGAR filings (reduced-depth dossier)"}

    block: dict = {}
    plans = (
        ("10k", "10-K", (("risk_factors", "1A", ("1B", "2")), ("mdna", "7", ("7A", "8")))),
        ("def14a", "DEF 14A", ()),
    )
    for key, form, item_specs in plans:
        try:
            meta = latest_filing_meta(cik10, form, run_id)
            if meta is None:
                block[key] = {"note": f"no {form} on record at EDGAR"}
                continue
            text = _fetch_filing_text(meta["url"], run_id)
            sections: dict = {}
            for name, start_item, end_items in item_specs:
                sections[name] = _section_payload(
                    extract_item_section(text, start_item, end_items), SECTION_BUDGETS[name]
                )
            if key == "def14a":
                for name, heading in _PROXY_HEADINGS.items():
                    sections[name] = _section_payload(
                        extract_proxy_block(text, heading), SECTION_BUDGETS[name]
                    )
            block[key] = {**meta, "sections": sections}
        except FetchError as exc:
            # Already recorded to fetch_log by the wrapped fetcher (Law 7); the pack
            # carries the outage explicitly instead of a silently absent key.
            block[key] = {"note": f"unavailable: {exc}"}
    return block


if __name__ == "__main__":
    import sys
    import uuid

    from analyst.cik import resolve_cik

    sym = (sys.argv[1] if len(sys.argv) > 1 else "TSLA").upper()
    rid = f"manual-filings-{uuid.uuid4().hex[:12]}"
    result = filings_block(sym, resolve_cik(sym, rid), rid)
    for fkey, fval in result.items():
        if "sections" not in (fval or {}):
            print(f"{fkey}: {fval}")
            continue
        print(f"{fkey}: {fval['form']} accn={fval['accn']} filed={fval['filed']}")
        for sname, sval in fval["sections"].items():
            if sval is None:
                print(f"  {sname}: NOT AVAILABLE")
            else:
                head = sval["text"][:200].replace("\n", " ")
                print(
                    f"  {sname}: {sval['chars_original']} chars"
                    f" (truncated={sval['truncated']})\n    {head}"
                )
