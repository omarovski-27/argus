"""Argus analyst — dossier synthesis + gates + store (module spec §1/§2/§4).

The flow (all-or-nothing; an ungated dossier never enters history, Law 2/7):

    build_data_pack -> run_valuation -> serialize_analysis (ONE labeled block)
    -> synthesis (config-driven model, 8-stage / 5-clause contract)
    -> ONE bounded repair pass when the draft fails a gate (violations named back
       to the model; draft rejection logged as ``analyst:draft``)
    -> parse + validate the VERDICTS block (controlled vocabulary, §2)
    -> Law-1 lint (analyst/law1.py)             } all fail loud, logged to
    -> numeric grounding vs the block (Law 2)    } fetch_log, blocking the
    -> claims-lint: superlatives vs series (L2)  } store (analyst:law1 /
    -> verdict-consistency vs the valuation      } :grounding / :claims / :verdicts)
    -> store to ``analyses`` (dossier + frozen pack+valuation + verdicts + model)

The synthesis model is config-driven (``config.synthesis_model``); Sonnet is the
default. Upgrading the model is a config upsert, never a deploy — and moving to a
pricier tier is an explicit Law-3 decision Omar makes, not a code change.

Run:  python -m analyst.dossier TSLA            (full run: synthesize, gate, store)
      python -m analyst.dossier TSLA --print-only   (synthesize + gates, NO store —
      the cheap iteration loop for prompt work)
      python -m analyst.dossier TSLA --send     (store, then deliver to Telegram —
      the /analyze workflow's step; store-first mirrors the digest pipeline, so a
      delivered dossier always exists in ``analyses`` even if the send then fails)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json
import os
import re
import time
import uuid

from anthropic import Anthropic
from dotenv import load_dotenv

from analyst.claims import _points, enforce_claims, validate_claims
from analyst.data_pack import build_data_pack
from analyst.law1 import BANNED_PATTERNS, CLOSING_LINE, enforce_law1, validate_law1
from analyst.rating import rating_from_verdicts, render_bottom_line
from analyst.serialize import serialize_analysis
from digest.grounding import validate_text
from quant.valuation import run_valuation
from shared.db import get_client
from shared.fetch_logger import elapsed_ms, write_fetch_log

DEFAULT_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 8000

# The §2 controlled vocabulary — the ONLY verdict tokens a dossier may render.
VERDICT_VOCAB = {
    "graham": {"CHEAP", "FAIR", "EXPENSIVE"},
    "buffett_business": {"Wonderful", "Good", "Mediocre"},
    "buffett_price": {"Discount", "Fair", "Premium"},
    "taleb": {"FRAGILE", "ROBUST", "ANTIFRAGILE"},
}

_VERDICTS_RE = re.compile(r"VERDICTS_JSON:\s*(\{.*\})\s*$", re.DOTALL)

# The 8-stage / 5-clause synthesis contract (module spec §1/§2 + blueprint §7).
# Law 1 and Law 2 are binding on every run; the two gates enforce them after.
# Examples stay ABSTRACT (a fact-shaped example gets emitted as data).
_SYSTEM = f"""You are writing an Argus analyst dossier — a fundamental-analysis brief on one company for one reader who makes his own capital decisions. You render framework verdicts; you never advise. Synthesize ONLY the DATA block into the fixed structure below.

You know these companies from training. Those priors are INADMISSIBLE here: if a specific figure is not printed in the DATA block, it does not exist for this dossier. That includes segment revenue splits, expense line items (R&D, SG&A, restructuring and the like), award and option counts, and any year-over-year change you would have to compute — even when you are confident you remember the true number, citing it is a violation.

Hard rules (all six bind on every dossier):
1. Grounding. Every number, date, and factual claim comes from the DATA block — filed figures, the derived metrics, the valuation-engine outputs, the filings excerpts, the consensus block, the news lines. Never supply a figure from memory, never estimate, and never compute a new number yourself (no sums, ratios, spreads, differences, growth rates, or percentages of your own — the block pre-computes what you may cite). The FILINGS TEXT excerpts are the ONLY filing content that exists for you: you know these companies from elsewhere, but a figure, date, or breakdown you remember from a filing and cannot point to in the excerpt is a fabrication here — segment revenue splits, option counts, award terms, vesting years and similar fine detail may be cited only if the excerpt itself states them. When you shorten a DATA figure, ROUND it at the last digit you display — never truncate (a cut-off digit makes it a different, wrong number); when unsure, quote the figure as the DATA prints it. Filing tables print figures in the table's stated units (often "in thousands" or "in millions"): cite a table figure with exactly the digits the excerpt prints, naming the unit in words right after it when the table declares one. Figures the DATA prints in full (the fundamentals table, the derived metrics, the valuation block) are cited with their digits as printed — never re-written into another denomination. Both directions, one rule: keep the DATA's own digits; only a unit word may be added beside them. When you want to combine two DATA numbers, cite each separately instead of the sum. When the structure calls for a comparison or trend the DATA does not pre-compute, describe its direction in words (higher/lower, widened/narrowed) with the underlying DATA figures — never a delta you derived. If something the structure calls for is not in the DATA, write "not available" and move on; a named gap is content, a filled gap is a violation.
2. No instructions. You never tell the reader to buy, sell, enter, exit, add, trim, accumulate, hold, or wait; no entry/exit points, no position sizes, no "attractive here", no timing language. Framework verdicts (cheap/expensive, wonderful/mediocre, fragile/antifragile) are analysis and required; anything that directs an action is forbidden. When the valuation block and the market price disagree, state the disagreement and what each side assumes — the reader owns the conclusion.
3. Fixed structure. Exactly the stages and sections listed below, in order, every dossier. Keep a stage's header even when its data is thin — say what is missing instead.
4. Interpretation beside every number. No bare figures: each number you cite gets its plain meaning in the same sentence, on its own scale, from the DATA's own anchors (a peer column, a prior year, a stated range). Do not grade magnitudes the DATA gives no anchor for. Scenario outputs are consequences of their stated assumptions — always present them WITH those assumptions, never as forecasts. SUPERLATIVES ARE DATA, NOT INFERENCE: any claim that a figure is a peak, high, low, trough, record, the highest/lowest, or that a series has risen/fallen "since" its extreme must come from the SERIES EXTREMA block, which pre-computes the true peak and trough (period and value) over ALL printed fiscal years. Never promote a recent or salient mid-series value to an extreme — the peak of the printed record is frequently an early year. If SERIES EXTREMA does not carry the concept, do not make the superlative claim at all.
5. Mark uncertainty. Say explicitly what is stale, missing, estimated by consensus, or resting on a labeled fallback basis (the DATA marks these). Reduced-depth areas are stated as such, plainly.
6. Plain language. Write for a sharp reader who is NOT a financial analyst. Every term of art is either replaced with plain English or glossed inline, in parentheses, the FIRST time it appears — then the short term may be used. Gloss at least these on first use: owner earnings (the cash the business generates after maintaining itself), reverse-DCF (working backwards from today's price to the growth it assumes), exit multiple (the price-to-earnings the market pays at the end), terminal margin (the profit margin assumed at the end of the forecast), CAGR (average yearly growth), margin of safety (the discount of price to a conservative value), dilution (the shrinking of each share's claim as new shares are issued). NEVER write "basis points" or "bps" — state changes in percentage points. Render figures the way a person speaks them: a large amount as "$X.X billion" rather than its full digit string, and a growth rate as "grew about N% a year over the last three years" rather than "a 3-year CAGR of N percent" (the technical term may follow in parentheses). Glossing and this human rounding NEVER change a figure's value — a spoken-rounded number must still be the DATA's own figure, rounded at the last digit you display (rule 1: round, never truncate). This clause is about wording only; it never licenses a number the DATA does not carry.

Structure (stage headers as plain CAPITALS lines):
STAGE 1 — BUSINESS. What it sells, who pays, why they keep paying, what breaks the repeat purchase — from the filings excerpts. If the business cannot be explained in one paragraph from the 10-K text provided, flag that itself.
STAGE 2 — FINANCIALS. The fiscal-year record: revenue trajectory and CAGRs, margin structure and trend, earnings consistency (loss years), cash generation (OCF, capex, FCF, owner earnings and its basis), balance-sheet posture (assets vs liabilities), share-count trend — dilution is a silent tax, flag it hard in either direction. Numbers from the DATA only.
STAGE 3 — MOAT & PEERS. The peer table read honestly: the pre-computed gross-margin spreads (cite the DATA's spread lines, never your own subtraction), growth vs peers, share-count discipline vs peers. Say what the spreads do and do not establish about a moat. Name peers with no ingestable fundamentals as gaps.
STAGE 4 — MANAGEMENT & BOARD. From the proxy excerpts: ownership, compensation structure and its incentives, anything the excerpts show about capital-allocation posture. What the excerpts do not show, say so.
STAGE 5 — STRATEGY & GUIDANCE POSTURE. From the MD&A excerpt: stated strategy, revealed priorities from the figures the DATA actually carries (the capex series; R&D or other spend lines ONLY where the excerpt itself states the number — the DATA does not carry filed R&D, so absent an excerpt figure the honest line is directional prose or "not available"). TAM claims treated skeptically. Guidance calibration only if the DATA carries it; otherwise "not available".
STAGE 6 — FRAGILITY AUDIT. Ruin risks and hidden fragilities from the risk-factors excerpt and the balance sheet; optionality on the antifragile side (net cash, multiple businesses, pipeline) where the DATA shows it. End the stage with the explicit list titled "What kills this company:".
STAGE 7 — VALUATION. Render the scenario table WITH its assumptions, the sensitivity read (name the biggest mover), any base-rate flag, the margin-of-safety line exactly as the DATA phrases it, and the reverse-DCF line — then set the implied growth against the actual filed growth record from Stage 2 and state the gap plainly. Trailing multiples with their dates. No price targets of your own.
STAGE 8 — MR. MARKET. The consensus and sentiment context as what is priced in and how the crowd leans — context, never evidence of value. Note where the crowd and the filed record diverge.

VERDICT BLOCK (after Stage 8, exactly these three lines then the three sections):
Graham: CHEAP or FAIR or EXPENSIVE — margin of safety as the DATA renders it, plus the one-line basis.
Buffett: (Wonderful or Good or Mediocre) business at a (Discount or Fair or Premium) price — one-line basis.
Taleb: FRAGILE or ROBUST or ANTIFRAGILE — followed by the ruin list in one line.
Then three honesty sections, each a short paragraph or list:
"What would change this verdict:" — explicit falsifiers tied to observable figures.
"What kills this company:" — restate the top ruin scenarios in one or two lines.
"Open questions for Omar:" — what this DATA could not resolve; seeds for the deep-dive session.

Close with exactly this line, verbatim, as the final line:
{CLOSING_LINE}

After that final line, append ONE machine line (it will be stripped before delivery):
VERDICTS_JSON: {{"graham": {{"verdict": "...", "margin_of_safety_pct": <number or null>}}, "buffett": {{"business": "...", "price": "..."}}, "taleb": {{"verdict": "...", "ruin_list": ["...", "..."]}}}}
The verdict tokens must be exactly from the sets above; margin_of_safety_pct is the DATA's percentage as a number, or null when the DATA renders it as not meaningful.

Length/tone: 1,200-1,800 words before the verdict block. Plain analyst prose — no hype, no hedging filler, no preamble, no meta-narration of your method. Confident about what the DATA shows, explicit about what it does not. Plain text only: no Markdown symbols (#, *, _, backticks, tables); stage headers as CAPITALS lines; blank lines between paragraphs."""


# The contract's own stage references — "STAGE 6 — FRAGILITY AUDIT" headers and
# "see Stage 7" cross-refs. Their digits are mandated STRUCTURE (clause 3), not data
# claims: on a sparse pack they ground to nothing, and the repair pass then deadlocks
# against the fixed-structure clause (the model may not delete headers — NTDOY probe).
# Masked before grounding; nothing else is. A rich block grounds them anyway.
_STAGE_REF_RE = re.compile(r"\bstage\s+[1-8]\b", re.IGNORECASE)
# SEC form NAMES are nomenclature, not figures — "a Form 20-F if filed" on a sparse
# pack flagged its '20' (NTDOY probe, round 3). Only the form-name token is blanked;
# figures beside it still validate.
_FORM_NAME_RE = re.compile(
    r"\b(?:form\s+)?(?:10-K|10-Q|20-F|8-K|6-K|DEF\s?14A|13[DFG])\b", re.IGNORECASE
)


def _mask_structural(text: str) -> str:
    """Blank the contract's stage references and SEC form names (same-length mask)."""
    masked = _STAGE_REF_RE.sub(lambda m: " " * len(m.group(0)), text)
    return _FORM_NAME_RE.sub(lambda m: " " * len(m.group(0)), masked)


# Filing tables declare their denomination ("(in thousands, except percentages)");
# the model reliably normalizes such figures to full dollars no matter how the prompt
# forbids it (PLTR: "$684,033,000" for the table's "684,033", three failed runs). A
# unit-normalized citation is the same equivalence class the gate already accepts via
# its B/M/K/T suffix tolerance ("16.5B" matches "16,500,000,000") — the failure was
# only that a bare full-dollar form has no adjacency link back to the table figure.
# So: for each filings SECTION that declares a denomination, that section's own
# comma-grouped figures join the grounding whitelist in expanded form. Whitelist-side
# only (the synthesis input is untouched), derived purely from the frozen pack (a
# stored row re-derives it — reproducibility holds), and it can only REDUCE false
# blocks, never admit a figure the excerpt doesn't carry.
_UNIT_DECL_RE = re.compile(r"\(in (thousands|millions)\b", re.IGNORECASE)
_COMMA_INT_RE = re.compile(r"(?<![\w.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_UNIT_MULT = {"thousands": 1e3, "millions": 1e6}


def _unit_expansion_whitelist(pack: dict) -> str:
    """Expanded (×1e3 / ×1e6) forms of figures printed in denomination-declaring
    filings sections — one number per line, for the grounding gate only."""
    values: set[float] = set()
    for form in (pack.get("filings") or {}).values():
        if not isinstance(form, dict):
            continue
        for section in (form.get("sections") or {}).values():
            text = (section or {}).get("text") or ""
            units = {m.group(1).lower() for m in _UNIT_DECL_RE.finditer(text)}
            if not units:
                continue
            raw = {float(m.group(0).replace(",", "")) for m in _COMMA_INT_RE.finditer(text)}
            for unit in units:
                values.update(v * _UNIT_MULT[unit] for v in raw)
    return "\n".join(f"{v:,.2f}" for v in sorted(values))


def validate_dossier_grounding(dossier_text: str, block: str, pack: dict) -> list[dict]:
    """The dossier's Law-2 check: structural refs masked, the unit-normalization
    whitelist appended to the block, then the shared digest.grounding rules. The one
    chokepoint the gate, the repair pass, and any stored-row probe all share."""
    extra = _unit_expansion_whitelist(pack)
    whitelist = block if not extra else (
        block + "\n\nUNIT-NORMALIZED FILINGS FIGURES (grounding whitelist only — "
        "the synthesis model never sees these lines):\n" + extra
    )
    return validate_text(_mask_structural(dossier_text), whitelist)


class VerdictParseError(RuntimeError):
    """The dossier's VERDICTS_JSON block is missing, malformed, or off-vocabulary."""


def _synthesis_model(client) -> str:
    """``config.synthesis_model`` when set, else the Sonnet default.

    A soft default is deliberate here (unlike sleeve_symbol): the model choice is
    an operational knob, not a registered gate — but CHANGING it via config is
    still the only path that doesn't require a deploy, and a paid-tier upgrade is
    an explicit Law-3 decision recorded by that config edit.
    """
    rows = (
        client.table("config").select("value").eq("key", "synthesis_model").limit(1).execute().data
        or []
    )
    value = rows[0]["value"] if rows else None
    return value if isinstance(value, str) and value.strip() else DEFAULT_MODEL


def _anthropic_key() -> str:
    load_dotenv(override=True)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY (see .env.example).")
    return key


def synthesize_dossier(block: str, model: str, repair_note: str | None = None) -> str:
    """One synthesis call over the serialized block; returns the raw dossier text.

    Temperature is pinned LOW: with a ~200 KB grounded context the failure mode is the
    model drifting toward its own priors (segment splits, expense lines it remembers);
    conservative sampling measurably reduces that drift. ``repair_note`` carries the
    bounded repair pass's violation list (see ``run_dossier``).
    """
    client = Anthropic(api_key=_anthropic_key())
    content = f"DATA:\n\n{block}"
    if repair_note:
        content += f"\n\n{repair_note}"
    message = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        temperature=0.2,
        system=_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text = next((b.text for b in message.content if b.type == "text"), "")
    if not text.strip():
        raise RuntimeError("dossier synthesis returned empty text")
    return text


def parse_verdicts(raw_text: str) -> tuple[str, dict]:
    """Split (dossier_text, verdicts) and enforce the §2 controlled vocabulary.

    The VERDICTS_JSON machine line is stripped from the delivered text; its tokens
    are validated against VERDICT_VOCAB — an off-vocabulary verdict is a Law-1
    boundary violation and fails the run (never stored).
    """
    m = _VERDICTS_RE.search(raw_text)
    if not m:
        raise VerdictParseError("no VERDICTS_JSON line found at the end of the dossier")
    try:
        verdicts = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise VerdictParseError(f"VERDICTS_JSON is not valid JSON: {exc}") from None

    problems: list[str] = []
    graham = (verdicts.get("graham") or {}).get("verdict")
    if graham not in VERDICT_VOCAB["graham"]:
        problems.append(f"graham.verdict {graham!r} not in {sorted(VERDICT_VOCAB['graham'])}")
    buffett = verdicts.get("buffett") or {}
    if buffett.get("business") not in VERDICT_VOCAB["buffett_business"]:
        problems.append(
            f"buffett.business {buffett.get('business')!r} not in "
            f"{sorted(VERDICT_VOCAB['buffett_business'])}"
        )
    if buffett.get("price") not in VERDICT_VOCAB["buffett_price"]:
        problems.append(
            f"buffett.price {buffett.get('price')!r} not in {sorted(VERDICT_VOCAB['buffett_price'])}"
        )
    taleb = verdicts.get("taleb") or {}
    if taleb.get("verdict") not in VERDICT_VOCAB["taleb"]:
        problems.append(f"taleb.verdict {taleb.get('verdict')!r} not in {sorted(VERDICT_VOCAB['taleb'])}")
    if not isinstance(taleb.get("ruin_list"), list) or not taleb.get("ruin_list"):
        problems.append("taleb.ruin_list must be a non-empty list")
    mos = (verdicts.get("graham") or {}).get("margin_of_safety_pct")
    if mos is not None and not isinstance(mos, (int, float)):
        problems.append("graham.margin_of_safety_pct must be a number or null")
    if problems:
        raise VerdictParseError("verdicts off-vocabulary: " + "; ".join(problems))

    dossier_text = raw_text[: m.start()].rstrip()
    return dossier_text, verdicts


def _verdict_problems(verdicts: dict, valuation: dict) -> list[str]:
    """Cross-check the machine verdicts against the valuation output (Law 2). Pure.

    The verdicts JSON is stored and re-rendered downstream, but the two text gates
    only see the PROSE — parse_verdicts strips the machine line first. So the two
    numbers-and-strings that ride in it are checked here: the Graham
    margin-of-safety percentage must be the valuation engine's own figure (null
    when the engine renders it not-meaningful — the serializer's n/m cap), and the
    ruin-list strings must pass the Law-1 instruction-shape patterns.
    """
    problems: list[str] = []
    mos = (verdicts.get("graham") or {}).get("margin_of_safety_pct")
    val_mos = valuation.get("margin_of_safety_pct") if valuation.get("renderable") else None
    if val_mos is None or val_mos < -1.0:
        if mos is not None:
            problems.append(
                f"graham.margin_of_safety_pct must be null — the valuation renders no "
                f"meaningful percentage — but got {mos}"
            )
    elif mos is None:
        problems.append(
            "graham.margin_of_safety_pct is null but the DATA renders a margin of safety"
        )
    elif abs(float(mos) - val_mos * 100.0) > 0.06:  # the block renders 1 decimal
        problems.append(
            f"graham.margin_of_safety_pct {mos} is not the DATA's margin of safety "
            f"({val_mos * 100.0:.1f})"
        )
    for item in (verdicts.get("taleb") or {}).get("ruin_list") or []:
        for rule, pattern in BANNED_PATTERNS:
            if pattern.search(str(item)):
                problems.append(f"ruin_list item is instruction-shaped [{rule}]: {item!r}")
    return problems


# The bottom-line rating (module spec §2, amended 2026-07-12) is INJECTED here — after
# synthesis and after all four gates — never generated by the model. Its sentence is
# recommendation-SHAPED by design ("not worth buying at today's price"); injecting it
# post-lint is exactly what lets the Law-1 gate keep banning that shape in the model's
# own prose while the framework's coded rating passes through (analyst/rating.py).
_VERDICT_ANCHOR_RE = re.compile(r"(?im)^[ \t]*(?:VERDICT\s+BLOCK\b|GRAHAM\s*:)")


def _bottom_line_price(pack: dict, valuation: dict) -> float | None:
    """The current price for the bottom-line render — from the frozen pack/valuation,
    never fabricated (matches the price the Stage-7 block already grounds)."""
    inp = valuation.get("inputs") if isinstance(valuation, dict) else None
    if isinstance(inp, dict) and isinstance(inp.get("price"), (int, float)):
        return float(inp["price"])
    close = (pack.get("price") or {}).get("close")
    return float(close) if isinstance(close, (int, float)) else None


def _inject_bottom_line(text: str, bottom_line: str) -> str:
    """Place ``bottom_line`` as the dossier's first line AND inside the verdict block.

    Pure string op. The verdict-block copy is inserted before the first verdict anchor
    (the 'VERDICT BLOCK' header when present, else the 'Graham:'/'GRAHAM:' line — both
    live formats); if no anchor is found the first-line copy still lands.
    """
    m = _VERDICT_ANCHOR_RE.search(text)
    if m:
        text = text[: m.start()] + bottom_line + "\n\n" + text[m.start():]
    return f"{bottom_line}\n\n{text}"


def finalize_dossier(
    dossier_text: str, verdicts: dict, symbol: str, price: float | None
) -> tuple[str, dict, str]:
    """Derive the rating, inject its sentence, and stamp verdicts (post-gate step).

    Returns (text_with_bottom_line, verdicts_with_rating, rating_token). Pure — the
    caller runs this only AFTER the four gates pass, so the injected recommendation
    shape never reaches the lint. The rating + its basis are added to ``verdicts`` for
    the stored row, so the bottom line re-derives forever (Law 2).
    """
    rating = rating_from_verdicts(verdicts)
    bottom_line = render_bottom_line(rating, symbol, price)
    text = _inject_bottom_line(dossier_text, bottom_line)
    verdicts = {**verdicts, "rating": rating.rating, "rating_basis": rating.rating_basis}
    return text, verdicts, rating.rating


# --------------------------------------------------------------------------- #
# Brief mode (module spec §3, amended 2026-07-12). The FULL dossier is ALWAYS
# synthesized, gated and stored; ``config.dossier_length`` selects only what Telegram
# receives. The brief is a DETERMINISTIC transform of the gated full text — verbatim
# for the qualitative prose the pack cannot reproduce (business, verdict lines, ruin
# list, open questions) plus code-rendered compacts of the numbers and valuation,
# grounded-by-construction from the frozen pack exactly like the serializer's
# derived-display figures. No second synthesis, no new gate, no new spend (Law 8).
# --------------------------------------------------------------------------- #
DOSSIER_LENGTHS = frozenset({"brief", "full"})
_STAGE_HDR_RE = re.compile(r"(?im)^STAGE\s+([1-8])\b[^\n]*$")
_VERDICT_LINE_RE = re.compile(r"(?im)^[ \t]*(graham|buffett|taleb)\s*:\s*\S.*$")


def resolve_dossier_length(client, override: str | None) -> str:
    """Delivery length: an explicit override wins, else ``config.dossier_length``.

    Fail-loud (Law 7): a missing/blank/off-vocabulary config row RAISES rather than
    guessing a format — the key is seeded (``analyst/seed_dossier_length.py``), so
    absence is a real misconfiguration to surface, not a default to paper over. This
    mirrors the sleeve-symbol resolver's refusal-to-guess, not the soft model default.
    """
    if override is not None:
        if override not in DOSSIER_LENGTHS:
            raise ValueError(
                f"dossier length override {override!r} not in {sorted(DOSSIER_LENGTHS)}"
            )
        return override
    rows = (
        client.table("config").select("value").eq("key", "dossier_length").limit(1)
        .execute().data or []
    )
    value = rows[0]["value"] if rows else None
    if isinstance(value, str) and value in DOSSIER_LENGTHS:
        return value
    raise RuntimeError(
        f"config.dossier_length is {value!r} — must be one of {sorted(DOSSIER_LENGTHS)}; "
        f"seed it with `python -m analyst.seed_dossier_length` (Law 7: no silent default)."
    )


def _usd(value) -> str:
    """A human money figure ('$94.8 billion'), rounded — brief prose only."""
    if not isinstance(value, (int, float)):
        return "not available"
    a = abs(value)
    if a >= 1e12:
        return f"${value / 1e12:.2f} trillion"
    if a >= 1e9:
        return f"${value / 1e9:.1f} billion"
    if a >= 1e6:
        return f"${value / 1e6:.1f} million"
    return f"${value:,.0f}"


def _ph(value, decimals: int = 1) -> str:
    """A human percent ('5.2%'), or 'not available'."""
    return "not available" if not isinstance(value, (int, float)) else f"{value * 100:.{decimals}f}%"


def _count_h(value) -> str:
    """A human count without a currency sign ('3.53 billion' shares) — same
    spoken-rounding rule as _usd, applied to share counts (plain-language clause)."""
    if not isinstance(value, (int, float)):
        return "not available"
    a = abs(value)
    if a >= 1e9:
        return f"{value / 1e9:.2f} billion"
    if a >= 1e6:
        return f"{value / 1e6:.1f} million"
    return f"{value:,.0f}"


def _stage_bodies(text: str) -> dict[int, str]:
    """{stage_number: body} split on the 'STAGE N ...' header lines (all live formats)."""
    heads = list(_STAGE_HDR_RE.finditer(text))
    bodies: dict[int, str] = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        bodies[int(m.group(1))] = text[m.start(): end].strip()
    return bodies


def _relabel(body: str, label: str) -> str:
    """Swap a stage body's header line for a plain ``label`` (drop 'STAGE 1 — ')."""
    rest = body.split("\n", 1)
    return f"{label}\n{rest[1].strip()}" if len(rest) > 1 else label


def _brief_verdicts(full_text: str, verdicts: dict) -> str:
    """The three verdict lines verbatim (case-insensitive — GRAHAM:/Graham: both live);
    falls back to a structured token render if fewer than three are found."""
    out: dict[str, str] = {}
    for m in _VERDICT_LINE_RE.finditer(full_text):
        key = m.group(1).lower()
        out.setdefault(key, m.group(0).strip())
    if len(out) >= 3:
        return "\n".join(out[k] for k in ("graham", "buffett", "taleb") if k in out)
    g = (verdicts.get("graham") or {}).get("verdict")
    b = verdicts.get("buffett") or {}
    t = (verdicts.get("taleb") or {}).get("verdict")
    return "\n".join([
        f"Graham: {g}.",
        f"Buffett: {b.get('business')} business at a {b.get('price')} price.",
        f"Taleb: {t}.",
    ])


def _brief_numbers(pack: dict) -> str:
    """The numbers that matter, code-rendered from the frozen pack (§3 list)."""
    metrics = pack.get("metrics") or {}
    lines: list[str] = []
    rev = sorted(_points(pack, ("series", "revenue", "value")))
    if rev:
        pe, v = rev[-1]
        lines.append(f"- Revenue: {_usd(v)} in FY {pe[:4]}.")
    cagr3 = ((metrics.get("revenue_cagr") or {}).get("3") or {}).get("value")
    if cagr3 is not None:
        lines.append(f"- Revenue growth: about {_ph(cagr3)} a year over the last three years.")
    gm = sorted(_points(pack, ("metrics", "margins", "gross_margin")))
    if gm:
        now_pe, now_v = gm[-1]
        peak_pe, peak_v = max(gm, key=lambda t: t[1])
        lines.append(
            f"- Gross margin: {_ph(now_v)} now (FY {now_pe[:4]}), against a peak of "
            f"{_ph(peak_v)} in FY {peak_pe[:4]}."
        )
    eps = sorted(_points(pack, ("metrics", "eps_history", "eps")))
    if eps:
        now_pe, now_v = eps[-1]
        peak_pe, peak_v = max(eps, key=lambda t: t[1])
        lines.append(
            f"- Earnings per share: {now_v:.2f} now (FY {now_pe[:4]}), against a peak of "
            f"{peak_v:.2f} in FY {peak_pe[:4]}."
        )
    fcf = sorted(_points(pack, ("metrics", "fcf_proxy", "fcf")))
    if fcf:
        pe, v = fcf[-1]
        lines.append(
            f"- Free cash flow (the cash left after keeping the business running): "
            f"{_usd(v)} in FY {pe[:4]}."
        )
    ta = sorted(_points(pack, ("series", "total_assets", "value")))
    tl = sorted(_points(pack, ("series", "total_liabilities", "value")))
    if ta and tl and ta[-1][0] == tl[-1][0]:
        pe, a = ta[-1]
        _, liab = tl[-1]
        lines.append(
            f"- Balance sheet: {_usd(a)} in assets against {_usd(liab)} in liabilities "
            f"({_usd(a - liab)} equity) at FY {pe[:4]}."
        )
    sh = sorted(_points(pack, ("series", "shares_diluted", "value")))
    if len(sh) >= 2:
        (pe0, s0), (pe1, s1) = sh[0], sh[-1]
        direction = "grew" if s1 > s0 else ("shrank" if s1 < s0 else "held flat")
        lines.append(
            f"- Diluted share count {direction} from {_count_h(s0)} (FY {pe0[:4]}) to "
            f"{_count_h(s1)} (FY {pe1[:4]}) — dilution is a silent tax."
        )
    return "\n".join(lines) if lines else "- not available"


def _brief_valuation(valuation: dict) -> str:
    """Valuation range + reverse-DCF gap in one or two plain sentences (§3)."""
    if not valuation.get("renderable"):
        return f"Valuation: not renderable — {valuation.get('reason')}."
    w = valuation.get("weighted_value_per_share")
    inp = valuation.get("inputs") or {}
    price = inp.get("price")
    mos = valuation.get("margin_of_safety_pct")
    rd = (valuation.get("reverse_dcf") or {}).get("implied_revenue_cagr")
    parts: list[str] = []
    if isinstance(w, (int, float)):
        parts.append(f"a conservative, bear-weighted value of about ${w:,.2f} a share")
    if isinstance(price, (int, float)):
        parts.append(f"against today's price of ${price:,.2f}")
    line = "Valuation: " + "; ".join(parts) if parts else "Valuation:"
    if isinstance(mos, (int, float)) and mos >= -1.0:
        line += f" — a margin of safety (discount of price to conservative value) of {_ph(mos)}"
    elif isinstance(price, (int, float)) and isinstance(w, (int, float)):
        line += " — the price sits above that conservative value, so there is no margin of safety"
    line += "."
    if isinstance(rd, (int, float)):
        line += (
            f" Working backwards from today's price, the market is assuming about "
            f"{_ph(rd)} revenue growth a year — weigh that against the filed record above."
        )
    return line


def _open_questions(full_text: str) -> str | None:
    """The 'Open questions for Omar:' section, up to the closing line."""
    idx = full_text.find("Open questions for Omar:")
    if idx == -1:
        return None
    tail = full_text[idx:]
    cut = tail.find(CLOSING_LINE)
    return (tail[:cut] if cut != -1 else tail).strip() or None


def render_brief(full_text: str, verdicts: dict, pack: dict, valuation: dict, symbol: str) -> str:
    """Assemble the ~600-900-word brief from the gated full text + frozen pack (§3).

    Graceful fallback: if the full text lacks the expected structure (no stage headers
    or no injected bottom line), deliver the full text unchanged — it is a superset, so
    the reader never loses data (Law 7: never hide, degrade openly).
    """
    stages = _stage_bodies(full_text)
    first = full_text.split("\n\n", 1)[0].strip()
    bottom = first if first.startswith("Bottom line:") else None
    if not stages or bottom is None:
        return full_text

    blocks = [bottom, "FRAMEWORK VERDICTS\n" + _brief_verdicts(full_text, verdicts)]
    if 1 in stages:
        blocks.append(_relabel(stages[1], "BUSINESS"))
    blocks.append("THE NUMBERS THAT MATTER\n" + _brief_numbers(pack))
    ruin = (verdicts.get("taleb") or {}).get("ruin_list") or []
    if ruin:
        blocks.append("WHAT KILLS THIS COMPANY:\n" + "\n".join(f"- {r}" for r in ruin))
    blocks.append(_brief_valuation(valuation))
    blocks.append(
        f"Moat and peers, management and board, strategy, and Mr. Market (Stages 3, 4, "
        f"5, 8): covered in the full dossier, which is stored — ask for any section with "
        f"/analyze {symbol} full."
    )
    oq = _open_questions(full_text)
    if oq:
        blocks.append(oq)
    blocks.append(CLOSING_LINE)
    return "\n\n".join(blocks)


def _draft_problems(dossier_text: str, block: str, pack: dict) -> list[str]:
    """Both gates' violations for a DRAFT, as repair-note lines (empty = clean draft).

    Pure composition of the two tested validators — this is the repair pass's input,
    not a gate: the binding gates run after it either way.
    """
    problems = [
        f"ungrounded figure {v['token']!r} in: ...{v['context']}..."
        for v in validate_dossier_grounding(dossier_text, block, pack)
    ]
    problems += [
        f"unsupported superlative — you called {c['concept']} {c['asserted_value']} "
        f"({str(c['asserted_period'])[:4]}) the {'peak' if c['direction'] == 'max' else 'low'}, "
        f"but SERIES EXTREMA shows {c['actual_value']} ({str(c['actual_period'])[:4]}): "
        f"...{c['excerpt']}..."
        for c in validate_claims(dossier_text, pack)
    ]
    problems += [
        f"instruction-shaped language [{v['rule']}]: ...{v['excerpt']}..."
        for v in validate_law1(dossier_text)
    ]
    return problems


class GateFailure(RuntimeError):
    """A post-synthesis gate (Law 1 lint / Law 2 grounding) rejected the dossier."""


def _gate(run_id: str, name: str, fn) -> None:
    """Run one gate: a failure is logged to fetch_log (Law 7) and re-raised."""
    start = time.monotonic()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — log, then propagate; nothing is stored
        write_fetch_log(f"analyst:{name}", run_id, "failure", elapsed_ms(start), str(exc)[:500])
        print(f"[dossier] GATE '{name}' FAILED — {exc}")
        raise GateFailure(f"{name}: {exc}") from exc
    write_fetch_log(f"analyst:{name}", run_id, "success", elapsed_ms(start))
    print(f"[dossier] gate '{name}' passed.")


def run_dossier(
    symbol: str,
    store: bool = True,
    send: bool = False,
    run_id: str | None = None,
    length: str | None = None,
) -> dict:
    """Build pack -> valuation -> synthesize -> gates -> (optionally) store + send.

    Returns {run_id, model, dossier_text, verdicts, rating, delivered_text, length,
    pack, valuation}. With ``store=False`` everything runs INCLUDING all gates but
    nothing is written to ``analyses`` — the cheap iteration loop for prompt work.
    ``send`` delivers the gated dossier to Telegram AFTER the store (digest-pipeline
    order: an undelivered dossier is recoverable from ``analyses``; a
    delivered-but-unstored one would break Law 2's reproducibility promise).

    The STORED text is ALWAYS the full dossier; ``length`` (an explicit 'brief'/'full'
    override, else ``config.dossier_length``) selects only what Telegram receives
    (module spec §3). The send is plain text — the synthesis contract already bans
    Markdown — and ``bot.telegram`` splits it at 4096.
    """
    run_id = run_id or f"dossier-{uuid.uuid4().hex[:12]}"
    client = get_client()
    sym = symbol.strip().upper()

    pack = build_data_pack(sym, run_id, client)
    valuation = run_valuation(pack, client=client)
    block = serialize_analysis(pack, valuation)
    model = _synthesis_model(client)
    print(f"[dossier] {sym}: block {len(block) / 1024:.1f} KB; model {model}; run {run_id}")

    start = time.monotonic()
    try:
        raw = synthesize_dossier(block, model)
    except Exception as exc:  # noqa: BLE001 — log, then propagate (Law 7)
        write_fetch_log("analyst:synthesis", run_id, "failure", elapsed_ms(start), str(exc)[:500])
        raise
    write_fetch_log("analyst:synthesis", run_id, "success", elapsed_ms(start))

    # ONE bounded repair pass (deterministic control flow, Law 8; the digest's §12
    # auto-retry is the precedent): if the draft fails the verdict parse or either
    # gate, name the exact violations back to the model and re-synthesize once. The
    # draft's rejection is logged (Law 7 — forensics on what the first pass got
    # wrong), the real gates below still run on whatever comes back, and they still
    # fail the run loud if the repair didn't take. An ungated dossier can never
    # reach store/send.
    try:
        dossier_text, verdicts = parse_verdicts(raw)
        problems = _draft_problems(dossier_text, block, pack) + _verdict_problems(verdicts, valuation)
    except VerdictParseError as exc:
        problems = [f"the VERDICTS_JSON machine line is invalid: {exc}"]
    if problems:
        write_fetch_log("analyst:draft", run_id, "failure", elapsed_ms(start), "; ".join(problems)[:500])
        print(f"[dossier] draft failed pre-gate ({len(problems)} problem(s)); one repair pass.")
        # The draft RIDES ALONG and the edit is minimal-diff by instruction: an early
        # repair variant that only named the problems made the model regenerate from
        # scratch, which INTRODUCED new violations (GM run: 1 draft problem -> 8).
        repair_note = (
            "REPAIR: your previous draft (below) violated the hard rules in the listed "
            "places. Reproduce the dossier with the SMALLEST edits that fix every "
            "listed problem: replace each offending figure/passage with the DATA "
            "block's own form (exactly as the DATA prints it) or with 'not available'. "
            "Keep every other sentence unchanged, keep the full structure, and end "
            "with the VERDICTS_JSON line.\nProblems:\n- "
            + "\n- ".join(problems)
            + "\n\nPREVIOUS DRAFT:\n" + raw
        )
        start = time.monotonic()
        try:
            raw = synthesize_dossier(block, model, repair_note=repair_note)
        except Exception as exc:  # noqa: BLE001
            write_fetch_log("analyst:repair", run_id, "failure", elapsed_ms(start), str(exc)[:500])
            raise
        write_fetch_log("analyst:repair", run_id, "success", elapsed_ms(start))
        try:
            dossier_text, verdicts = parse_verdicts(raw)
        except VerdictParseError as exc:
            # The run's TERMINAL failure must land in fetch_log (Law 7) — the job
            # alert points Omar there; an unlogged raise would leave only greens.
            write_fetch_log("analyst:verdicts", run_id, "failure", 0, str(exc)[:500])
            raise
    _gate(run_id, "law1", lambda: enforce_law1(dossier_text))

    def _grounding() -> None:
        violations = validate_dossier_grounding(dossier_text, block, pack)
        if violations:
            listed = "; ".join(f"{v['token']!r} ({v['context']!r})" for v in violations[:6])
            raise RuntimeError(
                f"{len(violations)} figure(s) not traceable to the DATA block: {listed}"
            )

    _gate(run_id, "grounding", _grounding)
    _gate(run_id, "claims", lambda: enforce_claims(dossier_text, pack))

    def _verdicts_gate() -> None:
        problems = _verdict_problems(verdicts, valuation)
        if problems:
            raise RuntimeError("; ".join(problems))

    _gate(run_id, "verdicts", _verdicts_gate)

    # Bottom-line rating: derived by CODE from the gated verdicts and injected here —
    # after every gate — so the recommendation-shaped sentence never reaches the lint
    # (module spec §2, amended 2026-07-12). The full text + verdicts now carry it.
    price = _bottom_line_price(pack, valuation)
    dossier_text, verdicts, rating = finalize_dossier(dossier_text, verdicts, sym, price)
    print(f"[dossier] bottom-line rating: {rating} at price {price}.")

    result = {
        "run_id": run_id,
        "symbol": sym,
        "model": model,
        "dossier_text": dossier_text,
        "verdicts": verdicts,
        "rating": rating,
        "pack": pack,
        "valuation": valuation,
    }
    if store:
        client.table("analyses").insert(
            {
                "symbol": sym,
                "run_id": run_id,
                # The dossier's EXACT frozen input: the pack plus the deterministic
                # valuation output (grid included) it was synthesized beside (Law 2).
                "data_pack_json": {"pack": pack, "valuation": valuation},
                "dossier_text": dossier_text,
                "verdicts": verdicts,
                "model": model,
            }
        ).execute()
        print(f"[dossier] stored analyses row for {sym} (run {run_id}).")
    else:
        print("[dossier] print-only: gates ran, nothing stored.")

    # Delivery shaping (§3): resolve the length only when it is actually needed (a
    # send, or an explicit override for a print-only preview) — a print-only run with
    # no override never touches the fail-loud config read.
    delivered_text = dossier_text
    if send or length is not None:
        resolved = resolve_dossier_length(client, length)
        delivered_text = (
            dossier_text if resolved == "full"
            else render_brief(dossier_text, verdicts, pack, valuation, sym)
        )
        result["length"] = resolved
        print(f"[dossier] delivery length: {resolved} ({len(delivered_text.split())} words).")
    result["delivered_text"] = delivered_text

    if send:
        from bot.telegram import send_message  # deferred: keep module import light

        start = time.monotonic()
        try:
            # Header carries NO digits: everything numeric in the delivery traces to the
            # frozen pack (gated full text, or brief compacts derived from it, Law 2);
            # a header date/number would be an ungrounded figure.
            send_message(f"ARGUS ANALYST DOSSIER — {sym}\n\n{delivered_text}", parse_mode="")
        except Exception as exc:  # noqa: BLE001 — log, then propagate (Law 7)
            write_fetch_log("analyst:telegram", run_id, "failure", elapsed_ms(start), str(exc)[:500])
            raise
        write_fetch_log("analyst:telegram", run_id, "success", elapsed_ms(start))
        print(f"[dossier] delivered dossier for {sym} to Telegram.")
    return result


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    # Length: --length brief|full, or a bare 'brief'/'full' positional (after the
    # ticker). None => resolve from config at delivery (only when sending).
    length = None
    if "--length" in sys.argv:
        i = sys.argv.index("--length")
        length = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    elif len(positional) > 1 and positional[1].lower() in ("brief", "full"):
        length = positional[1].lower()
    out = run_dossier(
        positional[0] if positional else "TSLA",
        store="--print-only" not in sys.argv,
        send="--send" in sys.argv and "--print-only" not in sys.argv,
        length=length,
    )
    print("\n" + "=" * 78 + "\n")
    print(out.get("delivered_text") or out["dossier_text"])
    print("\n" + "=" * 78)
    print(f"rating: {out.get('rating')}  |  length: {out.get('length', 'full (stored)')}")
    print(f"verdicts: {json.dumps(out['verdicts'], indent=1)}")
