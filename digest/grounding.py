"""Argus digest — numeric grounding validator (the Law 2 enforcer, post-synthesis).

Law 2 says every figure in any output renders from a stored DB row. The synthesis
prompt CLAIMS that; this module ENFORCES it: after Sonnet writes the digest and
before anything is stored or sent, every number in the synthesized text must be
traceable to the serialized bundle block (``digest.serialize.serialize_bundle`` of
the exact frozen ``bundle_json``) — the same block the model was shown. A number
that traces to nothing is a fabricated or model-computed figure; the run then FAILS
LOUD (Law 7): the violation is logged to ``fetch_log`` (``pipeline:grounding``, via
the pipeline's ``_critical`` wrapper) and the digest is neither stored nor sent.
Blocking the STORE too (not just the send) is deliberate: a digest that failed Law 2
must not become ``last_digest_sent_at`` or enter ``digests`` history as if delivered.

Derived-number allowance: anything the serializer itself computes (price Δ%, the VIX
trailing range/percentile, staleness ages, the source-health tally) appears in the
block, so it passes — while a figure the MODEL computed (a spread it derived, a sum,
an estimated level) appears nowhere in the block and fails. The whitelist is the
block itself, plus the intrinsic bounded scales the synthesis contract explicitly
permits citing (clauses 1 & 4: RSI/stochastics on 0-100, sentiment magnitude on 0-1)
— ``_SCALE_WHITELIST = {0, 1, 100}`` — and nothing else.

Matching is tolerance-aware, not string-equal: the model may round a block value to
the precision it displays ("17.6" for 17.63; "3.5B" for 3,528,000,000), so a text
token with d displayed decimals matches any block value within half a unit of that
last displayed digit (scaled through B/M/K/T suffixes). Dates match structurally:
an ISO date in the text must appear verbatim in the block; a prose date ("June 24")
is masked as grounded only when a block ISO date carries the same month/day(/year),
otherwise its digits fall through to plain numeric matching — the date parse can
only whitelist, never flag on its own (so "may 5 of 10 sessions" can't misfire).

Asymmetric label handling: digits embedded in words count as data on the BLOCK side
(RSI14 / SMA50 / 10Y-2Y contribute 14/50/10/2, so "the 50-day average" grounds) but
are NOT extracted as claims on the TEXT side (the model writing "RSI14" names an
indicator, it doesn't cite a datum).

Run:  python -m digest.grounding [digest_id]   (regression probe: validate a stored
      digest's full_text against its own frozen bundle_json; latest if no id)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import re

from digest.serialize import serialize_bundle

# Intrinsic bounded scales the synthesis contract explicitly permits citing without a
# block anchor (clauses 1 & 4): RSI/stochastics live on 0-100, sentiment magnitude on
# 0-1. 0 and 1 are in every block anyway (the headlines header prints "0-1"); 100 is
# the one genuinely extra allowance. Nothing else is pre-allowed.
_SCALE_WHITELIST: frozenset[float] = frozenset({0.0, 1.0, 100.0})

# Free-standing number: optional sign (ASCII / unicode minus / en-dash), then a
# comma-grouped, plain-decimal, bare-fraction or integer body. The lookbehind keeps
# digits embedded in words (RSI14) and decimal tails (the 63 of 17.63) from matching.
_NUM_RE = re.compile(
    r"""(?<![\w.,])
        [-+−–]?
        (?:
            \d{1,3}(?:,\d{3})+(?:\.\d+)?   # 3,528,000,000  /  1,234.56
          | \d+\.\d+                       # 17.63
          | \.\d+                          # .55
          | \d+                            # 62
        )
    """,
    re.VERBOSE,
)
# Digits attached to a letter (RSI14, SMA200, 10Y-2Y's 2Y) — block side only.
_EMBEDDED_NUM_RE = re.compile(r"(?<=[A-Za-z])(\d+(?:\.\d+)?)")
# Magnitude suffix immediately after a number: a bare capital B/M/K/T, or a word form —
# including the filing-table phrasing "X in thousands"/"X in millions" (same equivalence
# class as "X thousand": the candidate set only widens, the mantissa candidate remains).
_SUFFIX_RE = re.compile(
    r"^(?:(?P<letter>[BMKT])(?![a-zA-Z])|\s?(?:in\s+)?(?P<word>billions?|millions?|thousands?|trillions?|bn|mn|tn)\b)",
    re.IGNORECASE,
)
_SUFFIX_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12,
                "thousand": 1e3, "million": 1e6, "mn": 1e6,
                "billion": 1e9, "bn": 1e9, "trillion": 1e12, "tn": 1e12}

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
# "June 24" / "June 24th, 2026" and "24 June" / "24th of June 2026".
_PROSE_DATE_RE = re.compile(
    rf"\b(?P<month>{_MONTH_ALT})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{{4}}))?\b",
    re.IGNORECASE,
)
_PROSE_DATE_REV_RE = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+(?P<month>{_MONTH_ALT})\b(?:,?\s+(?P<year>\d{{4}}))?",
    re.IGNORECASE,
)

_CONTEXT_CHARS = 40   # context window around a violation, for the error message
_ERROR_MAX_CHARS = 500  # fetch_log error-string budget


class GroundingError(RuntimeError):
    """The synthesized text cites number(s) not traceable to the serialized bundle."""

    def __init__(self, violations: list[dict]):
        self.violations = violations
        listed = "; ".join(f"{v['token']!r} ({v['context']!r})" for v in violations)
        message = (
            f"digest failed numeric grounding (Law 2): {len(violations)} figure(s) not "
            f"traceable to the serialized bundle: {listed}"
        )
        if len(message) > _ERROR_MAX_CHARS:
            message = message[: _ERROR_MAX_CHARS - 12] + f"... (+{len(violations)} total)"
        super().__init__(message)


def _to_value(raw: str) -> float:
    """A matched number token's absolute numeric value (sign/commas normalized away)."""
    cleaned = raw.lstrip("+-−–").replace(",", "")
    return abs(float(cleaned))


def _decimals(raw: str) -> int:
    """Displayed decimal places of a matched token (0 for integers)."""
    body = raw.split(".", 1)
    return len(body[1]) if len(body) > 1 else 0


def _candidates(raw: str, tail: str) -> list[tuple[float, float]]:
    """(value, tolerance) pairs for one text token: the mantissa, plus its suffix
    expansion when a B/M/K/T (or word) magnitude immediately follows.

    Tolerance is half a unit of the last DISPLAYED digit — "17.6" tolerates
    |v - 17.6| <= 0.05, "3.5B" tolerates 0.05e9 — i.e. the block value must round to
    the text token at the token's own precision.
    """
    value = _to_value(raw)
    tol = 0.5 * (10.0 ** -_decimals(raw)) + 1e-9
    out = [(value, tol)]
    suffix = _SUFFIX_RE.match(tail)
    if suffix:
        key = (suffix.group("letter") or suffix.group("word")).lower()
        key = {"billions": "billion", "millions": "million", "thousands": "thousand",
               "trillions": "trillion"}.get(key, key)
        mult = _SUFFIX_MULT.get(key.upper() if len(key) == 1 else key)
        if mult:
            out.append((value * mult, tol * mult))
    return out


def _mask(text: str, start: int, end: int) -> str:
    """Blank a span (keeps every other match position stable)."""
    return text[:start] + " " * (end - start) + text[end:]


def _block_facts(block: str) -> tuple[list[float], set[str], set[tuple[int, int, int]]]:
    """What the serialized block grounds: numeric values, ISO date strings, (y,m,d) triples.

    Block-side extraction is deliberately permissive (label-embedded digits, suffix
    expansions, date years) — a wider allow-set only reduces false blocks; the text
    side stays strict.
    """
    values: list[float] = list(_SCALE_WHITELIST)
    iso_dates: set[str] = set()
    date_triples: set[tuple[int, int, int]] = set()

    for m in _ISO_DATE_RE.finditer(block):
        iso_dates.add(m.group(0))
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        date_triples.add((y, mo, d))
        values.append(float(y))  # "in 2026" / "Q2 2026" style year mentions ground here
    block = _ISO_DATE_RE.sub(lambda m: " " * len(m.group(0)), block)

    for m in _NUM_RE.finditer(block):
        for value, _tol in _candidates(m.group(0), block[m.end():m.end() + 16]):
            values.append(value)
    values.extend(float(m.group(1)) for m in _EMBEDDED_NUM_RE.finditer(block))
    return values, iso_dates, date_triples


def _prose_date_grounded(
    match: re.Match, triples: set[tuple[int, int, int]]
) -> bool:
    """True when a prose date names a (month, day[, year]) some block ISO date carries."""
    month = _MONTHS[match.group("month").lower()]
    day = int(match.group("day"))
    year = int(match.group("year")) if match.group("year") else None
    return any(
        mo == month and d == day and (year is None or y == year)
        for (y, mo, d) in triples
    )


def validate_text(full_text: str, block: str) -> list[dict]:
    """Every number/date in ``full_text`` not traceable to ``block`` (empty = grounded).

    Pure function of the two texts — no DB, no network — so a stored digest's
    (full_text, serialized bundle_json) pair reproduces its verdict forever.
    """
    values, iso_dates, triples = _block_facts(block)
    violations: list[dict] = []
    text = full_text

    def _context(start: int, end: int) -> str:
        return full_text[max(0, start - _CONTEXT_CHARS):end + _CONTEXT_CHARS].replace("\n", " ").strip()

    # 1) ISO dates: verbatim membership; flagged as a unit, then masked either way
    #    (a wrong date must not additionally flag as three numbers).
    for m in list(_ISO_DATE_RE.finditer(text)):
        if m.group(0) not in iso_dates:
            violations.append({"token": m.group(0), "context": _context(m.start(), m.end())})
        text = _mask(text, m.start(), m.end())

    # 2) Prose dates: whitelist-only — mask when grounded, else fall through to the
    #    numeric pass (never flag on the date parse itself).
    for pattern in (_PROSE_DATE_RE, _PROSE_DATE_REV_RE):
        for m in list(pattern.finditer(text)):
            if _prose_date_grounded(m, triples):
                text = _mask(text, m.start(), m.end())

    # 3) Free-standing numbers: any candidate (mantissa / suffix-expanded) within
    #    tolerance of any block value passes.
    for m in _NUM_RE.finditer(text):
        candidates = _candidates(m.group(0), text[m.end():m.end() + 16])
        if not any(abs(v - bv) <= tol for v, tol in candidates for bv in values):
            violations.append({"token": m.group(0), "context": _context(m.start(), m.end())})
    return violations


def validate_bundle(full_text: str, bundle: dict) -> list[dict]:
    """Violations of ``full_text`` against the serialized form of its frozen bundle."""
    return validate_text(full_text, serialize_bundle(bundle))


def enforce_grounding(full_text: str, bundle: dict) -> None:
    """Raise :class:`GroundingError` unless every figure in ``full_text`` is grounded.

    The digest pipeline runs this as a ``_critical`` step between synthesis and
    store/send: the raise is logged to ``fetch_log`` (``pipeline:grounding``) and
    aborts the run, so an ungrounded digest is never stored and never sent (Law 2/7).
    """
    violations = validate_bundle(full_text, bundle)
    if violations:
        raise GroundingError(violations)


# --------------------------------------------------------------------------- #
# Regression probe — validate a STORED digest against its own frozen bundle
# --------------------------------------------------------------------------- #
def _validate_stored(digest_id: int | None = None) -> list[dict]:
    from shared.db import get_client  # local import: the pure API stays DB-free

    client = get_client()
    query = client.table("digests").select("id,run_type,sent_at,full_text,bundle_json")
    if digest_id is None:
        rows = query.order("sent_at", desc=True).limit(1).execute().data or []
    else:
        rows = query.eq("id", digest_id).execute().data or []
    if not rows:
        print(f"[grounding] no stored digest found (id={digest_id})")
        return []
    row = rows[0]
    violations = validate_bundle(row["full_text"] or "", row["bundle_json"] or {})
    print(
        f"[grounding] digest id={row['id']} ({row['run_type']}, sent {row['sent_at']}): "
        f"{len(violations)} ungrounded figure(s)"
    )
    for v in violations:
        print(f"  FLAG {v['token']!r}  ...{v['context']}...")
    return violations


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — best-effort on non-reconfigurable streams
        pass
    _validate_stored(int(sys.argv[1]) if len(sys.argv) > 1 else None)
