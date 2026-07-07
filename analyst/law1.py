"""Argus analyst — the Law-1 lint (no instruction may survive to store/send).

Law 1 for the analyst module: framework VERDICTS are analysis and allowed
(cheap/expensive, wonderful/mediocre, fragile/antifragile); timing and sizing
INSTRUCTIONS are forbidden. The synthesis prompt claims that; this module
ENFORCES it, exactly as ``digest/grounding.py`` enforces Law 2 — post-synthesis,
pre-store, fail loud (the violation is logged to ``fetch_log`` as
``analyst:law1`` by the dossier flow, and the dossier is neither stored nor
sent).

Patterns match INSTRUCTION SHAPES, not bare trade words — the analytical
vocabulary legitimately contains "exit multiple", "share buybacks", "sell-side
consensus", "customers enter contracts", and consensus price targets from the
estimates block. A bare-word ban would false-positive on every dossier; a shape
that survives here should read as advice to a human too. The list errs toward
precision; the harsh-reader GATE (module spec §6) is the recall backstop.
"""

from __future__ import annotations

import re

# Each entry: (name, compiled pattern). Matched case-insensitively against the
# dossier text. Keep patterns HIGH-PRECISION: every addition needs a passing
# counter-example in tests/test_law1.py proving legitimate analysis still passes.
_TRADE_VERBS = r"(?:buy|sell|buying|selling|enter|exit|add|trim|accumulate|hold|wait)"

BANNED_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # Advice-verb constructions: "you should buy", "consider trimming",
    # "I recommend selling", "it may be worth adding", "time to exit".
    (
        "advice-verb construction",
        re.compile(
            rf"(?:\bshould\b|\brecommend(?:s|ed)?\b|\bconsider\b|\bsuggest(?:s|ed)?\b"
            rf"|\badvis(?:e|es|ed|able)\b|\bworth\b|\btime to\b|\burge(?:s|d)?\b)"
            rf"\W+(?:\w+\W+){{0,3}}?"
            rf"(?:buy|sell|buying|selling|enter(?!\s+(?:into\s+)?(?:contract|lease|agreement|market))"
            rf"|exit(?!\s+multiple)|add(?:ing)?\s+(?:to\s+)?(?:the\s+)?(?:position|shares|exposure|stake)"
            rf"|trim(?:ming)?|accumulat\w+|hold(?:ing)?\s+(?:the\s+)?(?:stock|shares|position))",
            re.IGNORECASE,
        ),
    ),
    # Imperatives at sentence start: "Buy now.", "Sell before earnings."
    (
        "imperative trade instruction",
        re.compile(
            rf"(?:^|[.!?:]\s+){_TRADE_VERBS}\b\s+(?:now|here|this|the\s+(?:stock|shares|dip)"
            rf"|before|at\s+\$?\d)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    # The explicit forbidden phrases (blueprint Law 1 / module spec Law 1).
    ("safe-to-trade language", re.compile(r"\bsafe\s+to\s+(?:trade|buy|sell|enter)\b", re.IGNORECASE)),
    (
        "timing call",
        re.compile(
            r"\b(?:now\s+is\s+(?:a\s+)?(?:good|bad|the)\s+time|good\s+time\s+to\s+\w+"
            r"|well[- ]timed|attractive\s+entry|(?:good|better|ideal)\s+entry"
            r"|entry\s+point|wait\s+for\s+(?:a\s+)?(?:pullback|dip|correction|better\s+price|lower\s+price))\b",
            re.IGNORECASE,
        ),
    ),
    # NOTE: these two end with (?!\w), not \b — a trailing \b after '%' or a digit
    # requires a WORD char next, so "allocate 30% of" and "enter at $250" would
    # silently never match (caught by tests/test_law1.py).
    (
        "sizing instruction",
        re.compile(
            r"\b(?:position\s+siz\w+|put\s+\d+\s*%|allocate\s+\d+\s*%|a\s+\d+\s*%\s+(?:position|allocation)"
            r"|size\s+(?:the|your)\s+position)(?!\w)",
            re.IGNORECASE,
        ),
    ),
    (
        "bracket/level instruction",
        re.compile(
            r"\b(?:stop[- ]loss\s+at|take\s+profits?|set\s+a\s+(?:stop|target|limit)"
            r"|enter\s+at\s+\$?\d+|exit\s+at\s+\$?\d+|buy\s+(?:below|under|at)\s+\$?\d+"
            r"|sell\s+(?:above|over|at)\s+\$?\d+)(?!\w)",
            re.IGNORECASE,
        ),
    ),
    (
        "colloquial trade nudge",
        re.compile(
            r"\b(?:back\s+up\s+the\s+truck|load\s+up|get\s+in\s+(?:now|before|early)"
            r"|get\s+out\s+(?:now|before|while)|don'?t\s+miss|pull\s+the\s+trigger)\b",
            re.IGNORECASE,
        ),
    ),
)

# The mandatory closing line (module spec §2) — its absence is a structural
# Law-1 failure too: the dossier must hand the decision back explicitly.
CLOSING_LINE = "Framework verdicts rendered. Timing and sizing are yours."

_CONTEXT_CHARS = 45


class Law1Error(RuntimeError):
    """The dossier text contains instruction-shaped language (Law 1)."""

    def __init__(self, violations: list[dict]):
        self.violations = violations
        listed = "; ".join(f"[{v['rule']}] {v['excerpt']!r}" for v in violations[:6])
        super().__init__(
            f"dossier failed the Law-1 lint: {len(violations)} instruction-shaped "
            f"passage(s): {listed}"
        )


def validate_law1(text: str) -> list[dict]:
    """Every instruction-shaped passage in ``text`` (empty list = clean). Pure."""
    violations: list[dict] = []
    for rule, pattern in BANNED_PATTERNS:
        for m in pattern.finditer(text):
            start, end = m.start(), m.end()
            excerpt = text[max(0, start - _CONTEXT_CHARS): end + _CONTEXT_CHARS]
            violations.append(
                {"rule": rule, "match": m.group(0), "excerpt": excerpt.replace("\n", " ").strip()}
            )
    if CLOSING_LINE not in text:
        violations.append(
            {
                "rule": "missing closing line",
                "match": "",
                "excerpt": f"dossier must end with: {CLOSING_LINE!r}",
            }
        )
    return violations


def enforce_law1(text: str) -> None:
    """Raise :class:`Law1Error` unless ``text`` passes the lint (mirror of enforce_grounding)."""
    violations = validate_law1(text)
    if violations:
        raise Law1Error(violations)
