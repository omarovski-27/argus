"""Argus analyst — the bottom-line rating (deterministic map of the three lenses).

Law 1 amended 2026-07-12 (Omar), dossier only: a dossier renders one summary
judgment — ATTRACTIVE / MIXED / UNATTRACTIVE "at current price" — on top of the
three framework verdicts. This module is that judgment, and it is **code, not the
model**: a pure function of the four lens tokens, so the bottom line cannot drift
with the model's mood (the run-to-run Mediocre/Good wobble that motivated this) and
re-derives forever from the stored ``analyses.verdicts`` (Law 2).

The rendered sentence is instruction-SHAPED by design ("not worth buying at today's
price"), which is exactly why it is injected by the pipeline AFTER the Law-1 lint
(``analyst/dossier.py``): the gate keeps banning recommendation language in
model-generated prose, while the framework's own coded rating passes through
untouched. Timing and sizing are still never rendered — the rating states
worth-at-a-price, never when to act or how much to hold.

Mapping rules (module spec §2), applied in order:
  1. graham == EXPENSIVE                                   -> UNATTRACTIVE
     (price fails regardless of quality — a wonderful business is not a buy at any
      price; the Buffett discipline.)
  2. graham == CHEAP and taleb != FRAGILE
     and buffett_quality in {Wonderful, Good}              -> ATTRACTIVE
  3. everything else                                       -> MIXED
     (the disagreeing lens is named in the render.)
"""

from __future__ import annotations

from dataclasses import dataclass

# The four lens vocabularies — mirror ``analyst.dossier.VERDICT_VOCAB`` (kept in sync
# by tests/test_rating.py). An off-vocabulary input is a programming error here (the
# verdicts are already gated to this vocabulary before the mapper runs), so it raises.
GRAHAM_VOCAB = frozenset({"CHEAP", "FAIR", "EXPENSIVE"})
QUALITY_VOCAB = frozenset({"Wonderful", "Good", "Mediocre"})
PRICE_VOCAB = frozenset({"Discount", "Fair", "Premium"})
TALEB_VOCAB = frozenset({"FRAGILE", "ROBUST", "ANTIFRAGILE"})

RATING_VOCAB = frozenset({"ATTRACTIVE", "MIXED", "UNATTRACTIVE"})


@dataclass(frozen=True)
class Rating:
    """A bottom-line rating plus the material to render it and store its basis.

    ``rating`` is the controlled-vocabulary token. ``clause`` is the failing-lens
    reason (UNATTRACTIVE) or the disagreement tension (MIXED); it is empty for
    ATTRACTIVE (the template carries its own affirmative clause). ``basis`` is the
    exact lens verdicts the rating was derived from — stored as
    ``analyses.verdicts.rating_basis`` so the bottom line re-derives forever.
    """

    rating: str
    clause: str
    basis: dict

    @property
    def rating_basis(self) -> dict:
        return dict(self.basis)


def _check(token: str, vocab: frozenset, name: str) -> str:
    if token not in vocab:
        raise ValueError(f"{name} {token!r} not in {sorted(vocab)}")
    return token


def _tension(graham: str, quality: str, taleb: str) -> str:
    """The MIXED disagreement clause — the price read, then the blocking lens(es)."""
    price_read = "the price looks cheap" if graham == "CHEAP" else (
        "the price is only fair (no clear margin of safety)"
    )
    blockers: list[str] = []
    if taleb == "FRAGILE":
        blockers.append("Taleb flags it as fragile (ruin-exposed)")
    if quality == "Mediocre":
        blockers.append("Buffett rates the business only mediocre")
    if blockers:
        return f"{price_read}, but {' and '.join(blockers)}"
    # graham == FAIR with quality/fragility both acceptable: no lens makes the case
    # AT THIS PRICE — the fair price is itself the reason there is no clean call.
    return (
        "the price is only fair, so no lens makes the case at this price — quality and "
        "fragility read acceptably but there is no margin of safety"
    )


def derive_rating(
    graham: str, buffett_quality: str, buffett_price: str, taleb: str
) -> Rating:
    """Map the four lens verdicts to a bottom-line rating (pure; ordered rules)."""
    _check(graham, GRAHAM_VOCAB, "graham")
    _check(buffett_quality, QUALITY_VOCAB, "buffett_quality")
    _check(buffett_price, PRICE_VOCAB, "buffett_price")
    _check(taleb, TALEB_VOCAB, "taleb")
    basis = {
        "graham": graham,
        "buffett_quality": buffett_quality,
        "buffett_price": buffett_price,
        "taleb": taleb,
    }

    if graham == "EXPENSIVE":
        return Rating(
            "UNATTRACTIVE",
            "Graham reads the price as expensive — above the conservative "
            "intrinsic-value range, and price discipline governs regardless of "
            "business quality",
            basis,
        )
    if graham == "CHEAP" and taleb != "FRAGILE" and buffett_quality in {"Wonderful", "Good"}:
        return Rating("ATTRACTIVE", "", basis)
    return Rating("MIXED", _tension(graham, buffett_quality, taleb), basis)


def rating_from_verdicts(verdicts: dict) -> Rating:
    """Derive the rating from a parsed ``verdicts`` dict (dossier §2 shape)."""
    graham = (verdicts.get("graham") or {}).get("verdict")
    buffett = verdicts.get("buffett") or {}
    taleb = (verdicts.get("taleb") or {}).get("verdict")
    return derive_rating(graham, buffett.get("business"), buffett.get("price"), taleb)


def render_bottom_line(rating: Rating, symbol: str, price: float | None) -> str:
    """The injected 'Bottom line:' sentence (module spec §2 templates).

    Pure render — the pipeline injects it AFTER the Law-1 lint. ``price`` comes from
    the frozen pack/valuation (never fabricated); when it is None the sentence drops
    the dollar figure but still names the rating.
    """
    t = symbol
    px = f"{price:,.2f}" if isinstance(price, (int, float)) else None
    of_px = f" of ${px}" if px else ""
    at_px = f"${px}" if px else "today's price"

    if rating.rating == "UNATTRACTIVE":
        return (
            f"Bottom line: by these frameworks, {t} is not worth buying at today's "
            f"price{of_px} — {rating.clause}."
        )
    if rating.rating == "ATTRACTIVE":
        return (
            f"Bottom line: by these frameworks, {t} is attractive at today's "
            f"price{of_px} — quality holds, fragility is manageable, and the price "
            f"sits below conservative value."
        )
    return (
        f"Bottom line: the frameworks disagree on {t} at {at_px} — {rating.clause}. "
        f"No clean call; the disagreement itself is the finding."
    )
