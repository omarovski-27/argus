"""Argus analyst — the bottom-line rating (deterministic map of the lenses + regime).

Law 1 amended 2026-07-12 (Omar), dossier only: a dossier renders one summary
judgment — ATTRACTIVE / MIXED / UNATTRACTIVE "at current price" — on top of the
three framework verdicts. This module is that judgment, and it is **code, not the
model**: a pure function of the lens tokens plus two grounded numbers, so the bottom
line cannot drift with the model's mood (the run-to-run Mediocre/Good wobble that
motivated this) and re-derives forever from the stored pack + verdicts (Law 2).

The rendered sentence is instruction-SHAPED by design ("not worth buying at today's
price"), which is exactly why it is injected by the pipeline AFTER the Law-1 lint
(``analyst/dossier.py``): the gate keeps banning recommendation language in
model-generated prose, while the framework's own coded rating passes through
untouched. Timing and sizing are still never rendered — the rating states
worth-at-a-price, never when to act or how much to hold.

MAPPER v2 (2026-07-13) — growth-aware regime gate. Graham's value lens systematically
tags every fast grower "EXPENSIVE" (a genuine growth company is never cheap on
trailing earnings), so on his own turf — a company actually growing revenue below a
config threshold — his veto stands (v1 rules, unchanged). But for a company growing
ABOVE the threshold, Graham is demoted to context and the rating turns on whether the
market's PRICE is demanding growth the company cannot plausibly deliver: the
reverse-DCF implied growth against the actual realized record (the gap ratio). That is
what makes the verdict "expensive for the right reason" rather than "expensive because
all growth is expensive to Graham".

Regime decision (config ``rating_growth_cagr_min``, seed 0.15):
  * revenue 3y CAGR >= threshold  -> GROWTH regime (gap-ratio rules below)
  * revenue 3y CAGR <  threshold  -> VALUE  regime (v1 lens rules; Graham keeps his veto)

Growth-regime rules (gap = implied_growth / max(actual_3y_cagr, 2% floor)):
  * gap >= ``rating_gap_extreme`` (seed 3.0)                 -> UNATTRACTIVE
  * gap <= ``rating_gap_ok`` (seed 1.5) and quality in
    {Wonderful, Good} and taleb != FRAGILE                  -> ATTRACTIVE
  * else                                                    -> MIXED (tension named)

Value-regime rules (v1, unchanged):
  1. graham == EXPENSIVE                                    -> UNATTRACTIVE
  2. graham == CHEAP and taleb != FRAGILE and quality in
     {Wonderful, Good}                                      -> ATTRACTIVE
  3. else                                                   -> MIXED (tension named)
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The four lens vocabularies — mirror ``analyst.dossier.VERDICT_VOCAB`` (kept in sync
# by tests/test_rating.py). An off-vocabulary input is a programming error here (the
# verdicts are already gated to this vocabulary before the mapper runs), so it raises.
GRAHAM_VOCAB = frozenset({"CHEAP", "FAIR", "EXPENSIVE"})
QUALITY_VOCAB = frozenset({"Wonderful", "Good", "Mediocre"})
PRICE_VOCAB = frozenset({"Discount", "Fair", "Premium"})
TALEB_VOCAB = frozenset({"FRAGILE", "ROBUST", "ANTIFRAGILE"})

RATING_VOCAB = frozenset({"ATTRACTIVE", "MIXED", "UNATTRACTIVE"})

# The three tunable thresholds, seeded via ``analyst/seed_rating_config.py``. A read
# helper (``load_rating_config``) surfaces corruption loudly; an absent key falls to
# these documented seeds (operational knobs, not a registered gate — so a soft default
# is deliberate, exactly like ``synthesis_model``, not the fail-loud sleeve symbol).
RATING_CONFIG_DEFAULTS: dict[str, float] = {
    "rating_growth_cagr_min": 0.15,
    "rating_gap_extreme": 3.0,
    "rating_gap_ok": 1.5,
}
# The reverse-DCF gap denominator floor: a company growing at 0% (or shrinking) still
# gets a finite, large gap rather than a divide-by-zero / negative blow-up (spec §1).
_CAGR_FLOOR = 0.02


@dataclass(frozen=True)
class Rating:
    """A bottom-line rating plus the material to render it and store its basis.

    ``rating`` is the controlled-vocabulary token. ``clause`` is the failing-lens
    reason (UNATTRACTIVE) or the disagreement tension (MIXED); it is empty for a
    value-regime ATTRACTIVE (the template carries its own affirmative clause), and
    non-empty for a growth-regime ATTRACTIVE (it supplies the gap reason). ``regime``
    is "growth" or "value" and drives the regime note in the render. ``basis`` is the
    exact inputs the rating was derived from — stored as
    ``analyses.verdicts.rating_basis`` so the bottom line re-derives forever.
    """

    rating: str
    clause: str
    basis: dict = field(default_factory=dict)
    regime: str = "value"

    @property
    def rating_basis(self) -> dict:
        return dict(self.basis)


def _check(token: str, vocab: frozenset, name: str) -> str:
    if token not in vocab:
        raise ValueError(f"{name} {token!r} not in {sorted(vocab)}")
    return token


def load_rating_config(client) -> dict[str, float]:
    """The three rating thresholds from ``config``; absent -> seed default, invalid -> raise.

    Fail-loud in the sense that matters (Law 7): a row present with a non-positive or
    non-numeric value is corruption and RAISES rather than silently steering the
    rating; an absent row falls to the documented seed (the keys are seeded, so absence
    is a fresh-DB state, not a bug). Pure read — no writes.
    """
    out = dict(RATING_CONFIG_DEFAULTS)
    rows = (
        client.table("config").select("key,value")
        .in_("key", list(RATING_CONFIG_DEFAULTS)).execute().data or []
    )
    seen = {r["key"]: r["value"] for r in rows}
    for key in RATING_CONFIG_DEFAULTS:
        if key not in seen:
            continue
        value = seen[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise RuntimeError(
                f"config.{key} is {value!r} — must be a positive number "
                f"(seed with `python -m analyst.seed_rating_config`)"
            )
        out[key] = float(value)
    return out


# --------------------------------------------------------------------------- #
# Value regime (v1) — Graham keeps his veto on his home turf
# --------------------------------------------------------------------------- #
def _value_tension(graham: str, quality: str, taleb: str) -> str:
    """The value-regime MIXED clause — the price read, then the blocking lens(es)."""
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
    return (
        "the price is only fair, so no lens makes the case at this price — quality and "
        "fragility read acceptably but there is no margin of safety"
    )


def _derive_value(graham: str, quality: str, taleb: str, basis: dict) -> Rating:
    """The v1 lens rules, unchanged — used for the value regime."""
    if graham == "EXPENSIVE":
        return Rating(
            "UNATTRACTIVE",
            "Graham reads the price as expensive — above the conservative "
            "intrinsic-value range, and price discipline governs regardless of "
            "business quality",
            basis,
            regime="value",
        )
    if graham == "CHEAP" and taleb != "FRAGILE" and quality in {"Wonderful", "Good"}:
        return Rating("ATTRACTIVE", "", basis, regime="value")
    return Rating("MIXED", _value_tension(graham, quality, taleb), basis, regime="value")


# --------------------------------------------------------------------------- #
# Growth regime — Graham demoted; the price's demanded growth vs the record
# --------------------------------------------------------------------------- #
def _gap_phrase(implied: float, actual: float, gap: float) -> str:
    """'~79%/yr against an actual ~5%/yr record (a gap of about 15x)' — the shared clause."""
    return (
        f"the price is pricing in about {implied * 100:.0f}%/yr revenue growth against an "
        f"actual {actual * 100:.1f}%/yr record over the last three years (a gap of about "
        f"{gap:.1f}x)"
    )


def _derive_growth(
    quality: str, taleb: str, implied: float | None, actual: float,
    gap_extreme: float, gap_ok: float, basis: dict,
) -> Rating:
    """Gap-ratio rules; when the reverse-DCF did not solve, fall back to the tension read."""
    if implied is None:
        # No implied growth (reverse-DCF unsolved): the gap is not computable, so the
        # growth judgment cannot run. Do not fabricate one — name it and let quality +
        # fragility carry a MIXED read (never silently borrow Graham's veto here).
        basis["gap_ratio"] = None
        return Rating(
            "MIXED",
            "the reverse-DCF did not solve, so the price's implied growth cannot be set "
            "against the actual record — no clean growth-adjusted call",
            basis,
            regime="growth",
        )
    denom = max(actual, _CAGR_FLOOR)
    gap = implied / denom
    basis["implied_growth"] = implied
    basis["gap_ratio"] = gap
    basis["gap_extreme"] = gap_extreme
    basis["gap_ok"] = gap_ok

    if gap >= gap_extreme:
        return Rating(
            "UNATTRACTIVE",
            f"{_gap_phrase(implied, actual, gap)} that leaves no room for error",
            basis,
            regime="growth",
        )
    if gap <= gap_ok and quality in {"Wonderful", "Good"} and taleb != "FRAGILE":
        return Rating(
            "ATTRACTIVE",
            f"{_gap_phrase(implied, actual, gap)} — within reach of the record, with "
            f"quality intact and fragility manageable",
            basis,
            regime="growth",
        )
    # Middle band, or a quality/fragility blocker — name whichever applies.
    blockers: list[str] = []
    if taleb == "FRAGILE":
        blockers.append("Taleb flags it as fragile (ruin-exposed)")
    if quality == "Mediocre":
        blockers.append("Buffett rates the business only mediocre")
    tail = (
        " — demanding but not extreme" if not blockers
        else f", and {' and '.join(blockers)}"
    )
    return Rating("MIXED", f"{_gap_phrase(implied, actual, gap)}{tail}", basis, regime="growth")


def derive_rating(
    graham: str,
    buffett_quality: str,
    buffett_price: str,
    taleb: str,
    *,
    revenue_cagr_3y: float | None = None,
    implied_growth: float | None = None,
    config: dict | None = None,
) -> Rating:
    """Map the lens verdicts + growth context to a bottom-line rating (pure; ordered).

    ``revenue_cagr_3y`` and ``implied_growth`` come from the frozen pack/valuation
    (the realized 3y revenue CAGR and the reverse-DCF implied growth). When
    ``revenue_cagr_3y`` is absent the mapper stays in the VALUE regime — the v1
    behaviour — so every existing caller (and the whole 81-combo table) is unchanged.
    """
    _check(graham, GRAHAM_VOCAB, "graham")
    _check(buffett_quality, QUALITY_VOCAB, "buffett_quality")
    _check(buffett_price, PRICE_VOCAB, "buffett_price")
    _check(taleb, TALEB_VOCAB, "taleb")
    cfg = config or RATING_CONFIG_DEFAULTS
    growth_min = cfg["rating_growth_cagr_min"]

    basis: dict = {
        "graham": graham,
        "buffett_quality": buffett_quality,
        "buffett_price": buffett_price,
        "taleb": taleb,
        "revenue_cagr_3y": revenue_cagr_3y,
        "growth_cagr_min": growth_min,
    }
    in_growth = isinstance(revenue_cagr_3y, (int, float)) and not isinstance(
        revenue_cagr_3y, bool
    ) and revenue_cagr_3y >= growth_min
    basis["regime"] = "growth" if in_growth else "value"

    if in_growth:
        return _derive_growth(
            buffett_quality, taleb, implied_growth, float(revenue_cagr_3y),
            cfg["rating_gap_extreme"], cfg["rating_gap_ok"], basis,
        )
    return _derive_value(graham, buffett_quality, taleb, basis)


def rating_from_verdicts(
    verdicts: dict,
    *,
    revenue_cagr_3y: float | None = None,
    implied_growth: float | None = None,
    config: dict | None = None,
) -> Rating:
    """Derive the rating from a parsed ``verdicts`` dict (dossier §2 shape) + growth context."""
    graham = (verdicts.get("graham") or {}).get("verdict")
    buffett = verdicts.get("buffett") or {}
    taleb = (verdicts.get("taleb") or {}).get("verdict")
    return derive_rating(
        graham, buffett.get("business"), buffett.get("price"), taleb,
        revenue_cagr_3y=revenue_cagr_3y, implied_growth=implied_growth, config=config,
    )


def _regime_note(rating: Rating) -> str:
    """The one-line regime statement appended to the bottom line (spec §1)."""
    if rating.regime == "growth":
        cagr = rating.basis.get("revenue_cagr_3y")
        grew = f" (revenue grew {cagr * 100:.1f}%/yr)" if isinstance(cagr, (int, float)) else ""
        return (
            "Rated on growth-adjusted basis — Graham's value lens is descriptive here, "
            f"not decisive{grew}."
        )
    return "Rated on value basis."


def render_bottom_line(rating: Rating, symbol: str, price: float | None) -> str:
    """The injected 'Bottom line:' sentence + regime note (module spec §2 templates).

    Pure render — the pipeline injects it AFTER the Law-1 lint. ``price`` comes from
    the frozen pack/valuation (never fabricated); when it is None the sentence drops
    the dollar figure but still names the rating.
    """
    t = symbol
    px = f"{price:,.2f}" if isinstance(price, (int, float)) else None
    of_px = f" of ${px}" if px else ""
    at_px = f"${px}" if px else "today's price"

    if rating.rating == "UNATTRACTIVE":
        line = (
            f"Bottom line: by these frameworks, {t} is not worth buying at today's "
            f"price{of_px} — {rating.clause}."
        )
    elif rating.rating == "ATTRACTIVE":
        if rating.clause:
            line = (
                f"Bottom line: by these frameworks, {t} is attractive at today's "
                f"price{of_px} — {rating.clause}."
            )
        else:
            line = (
                f"Bottom line: by these frameworks, {t} is attractive at today's "
                f"price{of_px} — quality holds, fragility is manageable, and the price "
                f"sits below conservative value."
            )
    else:
        line = (
            f"Bottom line: the frameworks disagree on {t} at {at_px} — {rating.clause}. "
            f"No clean call; the disagreement itself is the finding."
        )
    return f"{line} {_regime_note(rating)}"
