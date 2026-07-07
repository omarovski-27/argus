"""Argus analyst — dossier synthesis + gates + store (module spec §1/§2/§4).

The flow (all-or-nothing; an ungated dossier never enters history, Law 2/7):

    build_data_pack -> run_valuation -> serialize_analysis (ONE labeled block)
    -> synthesis (config-driven model, 8-stage / 5-clause contract)
    -> ONE bounded repair pass when the draft fails a gate (violations named back
       to the model; draft rejection logged as ``analyst:draft``)
    -> parse + validate the VERDICTS block (controlled vocabulary, §2)
    -> Law-1 lint (analyst/law1.py)          } both fail loud, logged to
    -> numeric grounding vs the block (Law 2) } fetch_log, blocking the store
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

from analyst.data_pack import build_data_pack
from analyst.law1 import BANNED_PATTERNS, CLOSING_LINE, enforce_law1, validate_law1
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

Hard rules (all five bind on every dossier):
1. Grounding. Every number, date, and factual claim comes from the DATA block — filed figures, the derived metrics, the valuation-engine outputs, the filings excerpts, the consensus block, the news lines. Never supply a figure from memory, never estimate, and never compute a new number yourself (no sums, ratios, spreads, differences, growth rates, or percentages of your own — the block pre-computes what you may cite). The FILINGS TEXT excerpts are the ONLY filing content that exists for you: you know these companies from elsewhere, but a figure, date, or breakdown you remember from a filing and cannot point to in the excerpt is a fabrication here — segment revenue splits, option counts, award terms, vesting years and similar fine detail may be cited only if the excerpt itself states them. When you shorten a DATA figure, ROUND it at the last digit you display — never truncate (a cut-off digit makes it a different, wrong number); when unsure, quote the figure as the DATA prints it. Filing tables print figures in the table's stated units (often "in thousands" or "in millions"): cite such figures EXACTLY as the excerpt prints them, with the table's own unit spelled out in words right after the number — NEVER expand them to full-dollar form and never convert between units (thousands to dollars, millions to billions); a figure written with more digits than the excerpt prints is a converted, computed number and a violation. When you want to combine two DATA numbers, cite each separately instead of the sum. When the structure calls for a comparison or trend the DATA does not pre-compute, describe its direction in words (higher/lower, widened/narrowed) with the underlying DATA figures — never a delta you derived. If something the structure calls for is not in the DATA, write "not available" and move on; a named gap is content, a filled gap is a violation.
2. No instructions. You never tell the reader to buy, sell, enter, exit, add, trim, accumulate, hold, or wait; no entry/exit points, no position sizes, no "attractive here", no timing language. Framework verdicts (cheap/expensive, wonderful/mediocre, fragile/antifragile) are analysis and required; anything that directs an action is forbidden. When the valuation block and the market price disagree, state the disagreement and what each side assumes — the reader owns the conclusion.
3. Fixed structure. Exactly the stages and sections listed below, in order, every dossier. Keep a stage's header even when its data is thin — say what is missing instead.
4. Interpretation beside every number. No bare figures: each number you cite gets its plain meaning in the same sentence, on its own scale, from the DATA's own anchors (a peer column, a prior year, a stated range). Do not grade magnitudes the DATA gives no anchor for. Scenario outputs are consequences of their stated assumptions — always present them WITH those assumptions, never as forecasts.
5. Mark uncertainty. Say explicitly what is stale, missing, estimated by consensus, or resting on a labeled fallback basis (the DATA marks these). Reduced-depth areas are stated as such, plainly.

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


def _draft_problems(dossier_text: str, block: str) -> list[str]:
    """Both gates' violations for a DRAFT, as repair-note lines (empty = clean draft).

    Pure composition of the two tested validators — this is the repair pass's input,
    not a gate: the binding gates run after it either way.
    """
    problems = [
        f"ungrounded figure {v['token']!r} in: ...{v['context']}..."
        for v in validate_text(_mask_structural(dossier_text), block)
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
    symbol: str, store: bool = True, send: bool = False, run_id: str | None = None
) -> dict:
    """Build pack -> valuation -> synthesize -> gates -> (optionally) store + send.

    Returns {run_id, model, dossier_text, verdicts, pack, valuation}. With
    ``store=False`` everything runs INCLUDING both gates but nothing is written to
    ``analyses`` — the cheap iteration loop for prompt work. ``send`` delivers the
    gated dossier to Telegram AFTER the store (digest-pipeline order: an undelivered
    dossier is recoverable from ``analyses``; a delivered-but-unstored one would
    break Law 2's reproducibility promise). The send is plain text — the synthesis
    contract already bans Markdown — and ``bot.telegram`` splits it at 4096.
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
        problems = _draft_problems(dossier_text, block) + _verdict_problems(verdicts, valuation)
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
        violations = validate_text(_mask_structural(dossier_text), block)
        if violations:
            listed = "; ".join(f"{v['token']!r} ({v['context']!r})" for v in violations[:6])
            raise RuntimeError(
                f"{len(violations)} figure(s) not traceable to the DATA block: {listed}"
            )

    _gate(run_id, "grounding", _grounding)

    def _verdicts_gate() -> None:
        problems = _verdict_problems(verdicts, valuation)
        if problems:
            raise RuntimeError("; ".join(problems))

    _gate(run_id, "verdicts", _verdicts_gate)

    result = {
        "run_id": run_id,
        "symbol": sym,
        "model": model,
        "dossier_text": dossier_text,
        "verdicts": verdicts,
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

    if send:
        from bot.telegram import send_message  # deferred: keep module import light

        start = time.monotonic()
        try:
            # Header carries NO digits: everything numeric in the delivery passed the
            # grounding gate; a header date/number would be an ungated figure (Law 2).
            send_message(f"ARGUS ANALYST DOSSIER — {sym}\n\n{dossier_text}", parse_mode="")
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = run_dossier(
        args[0] if args else "TSLA",
        store="--print-only" not in sys.argv,
        send="--send" in sys.argv and "--print-only" not in sys.argv,
    )
    print("\n" + "=" * 78 + "\n")
    print(out["dossier_text"])
    print("\n" + "=" * 78)
    print(f"verdicts: {json.dumps(out['verdicts'], indent=1)}")
