"""Argus analyst — the claims-lint (grounded-but-wrong superlatives; L2, post-grounding).

The numeric grounding gate proves every NUMBER traces to the block; ``law1`` proves no
INSTRUCTION survives. Neither can see a COMPARATIVE claim. "EPS peaked at 3.61 in FY
2022 and has since fallen" grounds — 3.61 and 2022 are both real pack points — yet is
false: the pack's EPS series is 3.61 / 4.30 / 2.03 / 1.08, so the peak is 4.30 at FY
2023. The superlative operator ("peaked") asserts ``3.61 == max(EPS)``; the model
manufactured an extremum from a mid-series value. That is a distinct Law-2 failure — a
GENERATED comparative fact — and it gets its own class-level enforcement here, after
grounding, exactly as grounding runs after synthesis.

Precision-first, mirroring ``law1`` (the harsh-reader recall backstop is the human
reader, not this gate): a claim FLAGS only when a superlative keyword, a concept
keyword, and a value that resolves to that concept's own series ALL sit inside one
window, AND the resolved value is not that concept's extremum in the claimed direction.
When any leg is missing (a superlative with no value, "the highest single mover" with no
figure, "filed record" as track-record) the claim is not checkable and is left alone.

The pack (``pack["series"]`` + ``pack["metrics"]``) is the frozen input the dossier was
synthesized from, so — like grounding reading the frozen bundle — a stored dossier's
(text, pack) pair reproduces this verdict forever.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Concept registry — where each fiscal series lives in the pack, its prose
# keywords, and whether it renders as a percentage (margins are stored as
# fractions but spoken as "25.6%"). label is for the flag message.
# --------------------------------------------------------------------------- #
_CONCEPTS: tuple[dict, ...] = (
    {"key": "revenue", "src": ("series", "revenue", "value"), "pct": False,
     "label": "revenue", "words": ("revenue", "sales", "top line", "top-line")},
    {"key": "gross_profit", "src": ("series", "gross_profit", "value"), "pct": False,
     "label": "gross profit", "words": ("gross profit",)},
    {"key": "operating_income", "src": ("series", "operating_income", "value"), "pct": False,
     "label": "operating income", "words": ("operating income", "operating profit")},
    {"key": "net_income", "src": ("series", "net_income", "value"), "pct": False,
     "label": "net income", "words": ("net income", "net loss", "net earnings")},
    {"key": "ocf", "src": ("series", "operating_cash_flow", "value"), "pct": False,
     "label": "operating cash flow", "words": ("operating cash flow",)},
    {"key": "capex", "src": ("series", "capex", "value"), "pct": False,
     "label": "capex", "words": ("capex", "capital expenditure", "capital spend")},
    {"key": "shares", "src": ("series", "shares_diluted", "value"), "pct": False,
     "label": "diluted shares", "words": ("diluted share", "share count", "shares outstanding")},
    {"key": "eps", "src": ("metrics", "eps_history", "eps"), "pct": False,
     "label": "EPS", "words": ("eps", "earnings per share", "per-share earnings")},
    {"key": "fcf", "src": ("metrics", "fcf_proxy", "fcf"), "pct": False,
     "label": "free cash flow", "words": ("free cash flow", "fcf", "owner earnings", "owner-earnings")},
    {"key": "gross_margin", "src": ("metrics", "margins", "gross_margin"), "pct": True,
     "label": "gross margin", "words": ("gross margin",)},
    {"key": "operating_margin", "src": ("metrics", "margins", "operating_margin"), "pct": True,
     "label": "operating margin", "words": ("operating margin",)},
    {"key": "net_margin", "src": ("metrics", "margins", "net_margin"), "pct": True,
     "label": "net margin", "words": ("net margin",)},
)

# Superlative operators, by the direction of the extremum they assert. Kept
# HIGH-PRECISION: "record" alone is excluded (track record / on record / filed
# record); only "record high"/"record low" carry an extremum, handled by the
# high/low members. Each addition needs a passing counter-example in the tests.
_MAX_WORDS = ("peaked", "peak", "highest", "all-time high", "record high", "strongest",
              "largest", "biggest", "maximum")
_MIN_WORDS = ("lowest", "trough", "bottomed", "weakest", "smallest", "record low",
              "minimum")
_SUPERLATIVE_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in (_MAX_WORDS + _MIN_WORDS)) + r")\b",
    re.IGNORECASE,
)
_MAX_SET = {w.lower() for w in _MAX_WORDS}

# A free-standing number in the claim window (sign, comma groups, decimals).
_NUM_RE = re.compile(r"(?<![\w.,])[-+]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+)")
_SUFFIX_RE = re.compile(
    r"^\s*(?:(?P<pct>%|\s*percent)|(?P<word>billion|million|thousand|trillion|bn|mn|tn)\b"
    r"|(?P<letter>[BMKT])(?![A-Za-z]))",
    re.IGNORECASE,
)
_SUFFIX_MULT = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6, "m": 1e6,
                "thousand": 1e3, "k": 1e3, "trillion": 1e12, "tn": 1e12, "t": 1e12}

_CONTEXT_CHARS = 55
# A superlative claims something within ITS OWN sentence. Scoping to the sentence (not a
# fixed char window) is what stops a claim binding to a concept in an ADJACENT sentence —
# the live GM class "...the series trough is -19.9% in FY 2012. Net margin peaked..." where
# a ±char window pulled "net margin" across the period into an operating-margin claim.
# Split on . ! ? followed by whitespace/EOL (so a decimal point mid-number never splits)
# or a newline.
_SENT_END_RE = re.compile(r"[.!?](?=\s|$)|\n")


def _sentence_span(text: str, kw_start: int, kw_end: int) -> tuple[int, int]:
    """(start, end) of the sentence containing the keyword at [kw_start, kw_end)."""
    start = 0
    for m in _SENT_END_RE.finditer(text[:kw_start]):
        start = m.end()
    nxt = _SENT_END_RE.search(text, kw_end)
    end = nxt.start() if nxt else len(text)
    return start, end
# A negative value spoken in words or a leading minus/dash just before the number, so
# "the trough of negative 19.9%" resolves to the stored -0.199 (else the extremum goes
# unrecognized and a nearby positive value is mis-flagged — the live GM false positive).
_NEG_BEFORE_RE = re.compile(r"(?:negative|minus|[-–−])\s*$", re.IGNORECASE)


class ClaimsError(RuntimeError):
    """A dossier asserts an extremum a pack series does not support (Law 2)."""

    def __init__(self, violations: list[dict]):
        self.violations = violations
        listed = "; ".join(
            f"[{v['concept']} {v['direction']}] {v['excerpt']!r} — actual "
            f"{v['actual_value']} at {v['actual_period']}"
            for v in violations[:6]
        )
        super().__init__(
            f"dossier failed the claims-lint: {len(violations)} superlative(s) not "
            f"supported by the pack series: {listed}"
        )


def _points(pack: dict, src: tuple) -> list[tuple[str, float]]:
    """(period_end, value) pairs for one concept, dropping None/undated rows."""
    container, key, field = src
    rows = ((pack.get(container) or {}).get(key)) or []
    out: list[tuple[str, float]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        v, pe = r.get(field), r.get("period_end")
        if v is not None and pe:
            try:
                out.append((str(pe), float(v)))
            except (TypeError, ValueError):
                continue
    return out


def _decimals(raw: str) -> int:
    body = raw.split(".", 1)
    return len(body[1]) if len(body) > 1 else 0


def _candidate_values(token: str, tail: str, is_pct: bool) -> list[tuple[float, float]]:
    """(value, tolerance) candidates for a text number, scale-aware.

    A '%'/'percent' suffix (or a pct concept) divides by 100; a B/M/K/T word or
    letter suffix multiplies. Tolerance is half a unit of the last displayed digit,
    scaled through the same factor — so "25.6%" matches a stored 0.256 and "8,527
    million" matches 8,527,000,000.
    """
    value = abs(float(token.lstrip("+-").replace(",", "")))
    tol = 0.5 * (10.0 ** -_decimals(token)) + 1e-9
    m = _SUFFIX_RE.match(tail)
    pct = is_pct
    mult = 1.0
    if m:
        if m.group("pct"):
            pct = True
        else:
            key = (m.group("word") or m.group("letter")).lower()
            mult = _SUFFIX_MULT.get(key, 1.0)
    if pct:
        return [(value / 100.0, tol / 100.0)]
    if mult != 1.0:
        return [(value * mult, tol * mult), (value, tol)]
    return [(value, tol)]


def _disp(value: float, is_pct: bool) -> str:
    return f"{value * 100:.1f}%" if is_pct else f"{value:,.2f}"


def validate_claims(text: str, pack: dict) -> list[dict]:
    """Every unsupported superlative in ``text`` (empty list = clean). Pure.

    For each superlative, for each concept named in its window, resolve EVERY number in
    the window (sign-aware) to that concept's series. If the true extremum is among the
    resolved values, the claim cites it correctly and PASSES — this is what makes a
    legitimate comparative ("1.6%, well above the trough of negative 19.9%") clean. Only
    when no resolved value is the extremum AND a non-extremum value is bound to the
    superlative is it flagged (a mid-series value dressed as the peak/low).
    """
    concepts = [{**c, "points": _points(pack, c["src"])} for c in _CONCEPTS]
    concepts = [c for c in concepts if c["points"]]
    if not concepts:
        return []
    violations: list[dict] = []

    for sm in _SUPERLATIVE_RE.finditer(text):
        want_max = sm.group(1).lower() in _MAX_SET
        lo, hi = _sentence_span(text, sm.start(), sm.end())
        window = text[lo:hi]
        wl = window.lower()
        named = [c for c in concepts if any(w in wl for w in c["words"])]
        if not named:
            continue  # superlative not bound to a known series — not checkable
        # A superlative binds to ONE concept — the one whose value sits nearest the
        # keyword. Checking EVERY named concept false-flags a contrast clause: "OCF
        # reached a record 26,867M — the highest — while capex of 9,303M" claims OCF,
        # not capex (live GM false positive). Resolve each named concept, keep its
        # nearest value + whether its extremum is cited, then judge only the nearest.
        bound = None  # (nearest_dist, concept, period, value, extremum_cited)
        for c in named:
            extremum = (max if want_max else min)(v for _, v in c["points"])
            resolved: list[tuple[int, str, float, bool]] = []  # dist, period, value, is_ext
            for nm in _NUM_RE.finditer(window):
                tail = window[nm.end(): nm.end() + 12]
                sign = -1.0 if _NEG_BEFORE_RE.search(window[max(0, nm.start() - 10): nm.start()]) else 1.0
                dist = abs((lo + nm.start()) - sm.start())
                for cand, tol in _candidate_values(nm.group(0), tail, c["pct"]):
                    hits = [(pe, v) for pe, v in c["points"] if abs(v - cand * sign) <= tol]
                    if hits:
                        pe, v = hits[0]
                        resolved.append((dist, pe, v, abs(v - extremum) <= 1e-6))
                        break
            if not resolved:
                continue  # names the concept but cites no series value
            near = min(resolved, key=lambda t: t[0])
            extremum_cited = any(is_ext for *_, is_ext in resolved)
            if bound is None or near[0] < bound[0]:
                bound = (near[0], c, near[1], near[2], extremum_cited)
        if bound is None:
            continue
        _dist, c, mp, mv, extremum_cited = bound
        if extremum_cited:
            continue  # the bound concept cites its true extremum in the window — correct
        extremum = (max if want_max else min)(v for _, v in c["points"])
        ext_period = next((pe for pe, v in c["points"] if abs(v - extremum) <= 1e-6), "?")
        violations.append({
            "concept": c["label"],
            "direction": "max" if want_max else "min",
            "asserted_value": _disp(mv, c["pct"]),
            "asserted_period": mp,
            "actual_value": _disp(extremum, c["pct"]),
            "actual_period": ext_period,
            "excerpt": text[max(0, sm.start() - _CONTEXT_CHARS): sm.end() + _CONTEXT_CHARS]
            .replace("\n", " ").strip(),
        })
    return violations


def enforce_claims(text: str, pack: dict) -> None:
    """Raise :class:`ClaimsError` unless every superlative is series-supported."""
    violations = validate_claims(text, pack)
    if violations:
        raise ClaimsError(violations)
