"""Argus ingestion — SEC XBRL company-facts mapper (11 fundamental concepts).

Writes into the three-layer fundamentals schema (``fundamentals`` ->
``fundamentals_latest`` view -> ``corporate_actions``). Generalized from the proven
revenue-only first cut to a CONCEPT REGISTRY (:data:`CONCEPTS`): one
``concept -> tag-priority`` mapping drives the same fetch -> filter -> map -> upsert
pipeline for every fundamental.

Source (free, no key — SEC EDGAR):
    GET https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json
    Auth: none. SEC asks for a descriptive ``User-Agent`` carrying a contact
    address (their fair-access policy) — sent on every request, no secret involved.
    Response: one large JSON document holding every tag the issuer has filed. We
    fetch it ONCE per run and slice each concept's tag/unit out of it — concepts
    are columns of the same document, not separate HTTP calls (one call, §12).

The four rules (unchanged from the revenue cut):

1. Annual-10-K filter. A concept is either a ``duration`` flow (income statement /
   cash flow: a fiscal-YEAR period — keep ``form == '10-K'`` AND ``end - start`` in
   350-380 days, dropping quarters/stubs) or an ``instant`` snapshot (balance sheet
   and the dei share count: a point-in-time fact with an ``end`` instant and NO
   ``start`` — keep ``form == '10-K'``, ``period_start`` stays NULL). Balance-sheet
   tags (Assets, Liabilities) and EntityCommonStockSharesOutstanding are ``instant``
   in XBRL; a 350-380-day window would drop every one of them.
2. Key on the actual period (``period_start``/``period_end``), never on fy/fp (which
   collide across amendments). Instant facts key on ``period_end`` alone.
3. Insert-all: every surviving filing's own version is kept — no dedup on write. The
   ``fundamentals_latest`` view (DISTINCT ON, latest ``filed`` wins) resolves the
   "current value" at read time.
4. Traceability triple: every row carries ``accn`` / ``form`` / ``filed`` (which
   filing it came from) plus ``raw_json`` (the SEC data point verbatim).

Tag priority = FILL-GAP, never union-sum (Law 2). A concept may list several tags in
priority order; a later tag contributes a data point ONLY for a fiscal period that no
earlier tag already supplied. Values are never summed or blended across tags — the
list patches coverage holes (e.g. operating_cash_flow's 2nd tag fills the 2014-2015
years the 1st tag is missing), it does not aggregate.

What it writes (Law 2 — facts retrieved, never generated): exactly the data points
present in the JSON, mapped 1:1 to ``fundamentals`` rows. No interpolation, no
gap-fill within a tag — a year SEC never filed under a concept's tags stays missing.

Idempotent: the upsert is keyed on the unique constraint
(symbol, concept, tag, period_start, period_end, accn) with DO-NOTHING on conflict
(``ignore_duplicates=True``). The index is NULLS NOT DISTINCT, so instant rows
(period_start NULL) dedupe too — re-running inserts nothing new. The reported
"inserted" count is a true before/after row-count delta, not the upsert echo.

All HTTP goes through :func:`shared.fetcher_base.fetch_with_retry` (the §12
reliability contract + ``fetch_log``); no bare httpx calls.

Run:  python -m ingestion.sec_facts   (or: python ingestion/sec_facts.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from dataclasses import dataclass
from datetime import date, datetime, timezone

from shared.db import get_client
from shared.exceptions import FetchError
from shared.fetcher_base import fetch_with_retry

# Default issuer (the original single-issuer cut). Every function below now takes
# symbol/cik parameters so the analyst module can ingest peer issuers through the
# SAME proven pipeline (analyst-module §1 Stage 3); these defaults keep the original
# TSLA probe (`python -m ingestion.sec_facts`) and all existing callers unchanged.
SYMBOL = "TSLA"
CIK = "0001318605"  # Tesla, Inc. — SEC company-facts is keyed on the 10-digit CIK.
COMPANY_FACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
COMPANY_FACTS_URL = COMPANY_FACTS_URL_TEMPLATE.format(cik=CIK)

# SEC's fair-access policy asks for a descriptive UA with a contact address; this
# is a courtesy header, not a credential (so it is fine in code / fetch_log).
USER_AGENT = "Argus portfolio-intelligence (omar.aloran27@yahoo.com)"

# Annual-period gate (duration concepts): a 10-K fiscal year is ~365 days; allow
# 52/53-week filers and fiscal-calendar drift (350-380) while excluding quarters
# (~90d) and odd stubs.
ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380
ANNUAL_FORM = "10-K"

# Period kinds.
DURATION = "duration"  # income-statement / cash-flow flow over a fiscal year.
INSTANT = "instant"    # balance-sheet / point-in-time snapshot (period_start NULL).


@dataclass(frozen=True)
class Concept:
    """One fundamental: which XBRL tag(s) supply it, and how it is shaped.

    tags is a PRIORITY list (fill-gap, never union-sum — see module docstring).
    namespace is the company-facts top-level facts key ('us-gaap' or 'dei').
    """

    name: str
    tags: tuple[str, ...]
    namespace: str  # 'us-gaap' | 'dei'
    unit: str  # 'USD' | 'shares'
    period_kind: str  # DURATION | INSTANT
    is_split_adjustable: bool


# The 11-concept registry. Order is purely cosmetic (report order).
CONCEPTS: tuple[Concept, ...] = (
    # revenue: 'Revenues' first (TSLA/GM/F consolidated line), then the ASC 606 tag
    # (RIVN files ONLY that one; probed 2026-07-05), then the pre-606 legacy tag for
    # deep history. Fill-gap: issuers already covered by 'Revenues' take nothing new.
    Concept(
        "revenue",
        (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
        ),
        "us-gaap",
        "USD",
        DURATION,
        False,
    ),
    # net_income: NetIncomeLoss only — never ProfitLoss (a different, broader tag).
    Concept("net_income", ("NetIncomeLoss",), "us-gaap", "USD", DURATION, False),
    Concept("operating_income", ("OperatingIncomeLoss",), "us-gaap", "USD", DURATION, False),
    # Balance-sheet snapshots: instant facts (no start), period_start stays NULL.
    Concept("total_assets", ("Assets",), "us-gaap", "USD", INSTANT, False),
    Concept("total_liabilities", ("Liabilities",), "us-gaap", "USD", INSTANT, False),
    # cost_of_revenue: GM/F/RIVN file CostOfGoodsAndServicesSold, not CostOfRevenue
    # (probed 2026-07-05) — the 2nd tag fills those issuers entirely.
    Concept(
        "cost_of_revenue",
        ("CostOfRevenue", "CostOfGoodsAndServicesSold"),
        "us-gaap",
        "USD",
        DURATION,
        False,
    ),
    Concept("gross_profit", ("GrossProfit",), "us-gaap", "USD", DURATION, False),
    # operating_cash_flow: the 2nd tag fills only the years the 1st never filed
    # (TSLA's 2014-2015 gap) — fill-gap, never summed.
    Concept(
        "operating_cash_flow",
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
        "us-gaap",
        "USD",
        DURATION,
        False,
    ),
    # capex: the FCF-proxy subtrahend (FCF = OCF - capex; analyst module cash-flow
    # framework). Tesla's primary tag is PaymentsToAcquirePropertyPlantAndEquipment;
    # the 2nd tag fills any early years filed under the broader ProductiveAssets line.
    Concept(
        "capex",
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
        ),
        "us-gaap",
        "USD",
        DURATION,
        False,
    ),
    # depreciation_amortization: the owner-earnings add-back (OE = NI + D&A - capex,
    # valuation engine / module spec Stage 7). Both tags mean FULL D&A (the 2nd fills
    # TSLA's early years); bare 'Depreciation' is deliberately excluded — it is a
    # semantically narrower fact and fill-gap would silently mix it into the series.
    # All 5 current issuers file the primary tag (probed 2026-07-05).
    Concept(
        "depreciation_amortization",
        ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization"),
        "us-gaap",
        "USD",
        DURATION,
        False,
    ),
    # Share counts: split-adjustable, unit 'shares'.
    Concept(
        "shares_diluted",
        ("WeightedAverageNumberOfDilutedSharesOutstanding",),
        "us-gaap",
        "shares",
        DURATION,
        True,
    ),
    # shares_outstanding lives in the dei namespace and is point-in-time (the cover
    # -page count "as of" a date) — instant, period_start NULL.
    Concept(
        "shares_outstanding",
        ("EntityCommonStockSharesOutstanding",),
        "dei",
        "shares",
        INSTANT,
        True,
    ),
)

UPSERT_CONFLICT = "symbol,concept,tag,period_start,period_end,accn"


def _new_run_id(prefix: str) -> str:
    """A timestamped run id for a manual run (groups its fetch_log rows)."""
    return f"{prefix}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"


def _period_days(data_point: dict) -> int | None:
    """Whole days from ``start`` to ``end`` for a data point, or None if unparseable."""
    start, end = data_point.get("start"), data_point.get("end")
    if not start or not end:
        return None
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _survives(data_point: dict, kind: str) -> bool:
    """True iff the data point is the annual 10-K figure this concept keeps.

    DURATION: a 10-K fiscal-year period (form 10-K AND 350-380 days).
    INSTANT:  a 10-K point-in-time fact (form 10-K AND an ``end`` instant, no start).
    """
    if data_point.get("form") != ANNUAL_FORM:
        return False
    if kind == DURATION:
        days = _period_days(data_point)
        return days is not None and ANNUAL_MIN_DAYS <= days <= ANNUAL_MAX_DAYS
    # INSTANT: balance-sheet / point-in-time — no period length to test.
    return data_point.get("start") is None and data_point.get("end") is not None


def _period_key(data_point: dict, kind: str) -> tuple[str | None, str]:
    """The fiscal-period identity used for fill-gap (instant facts: end only)."""
    if kind == DURATION:
        return (data_point["start"], data_point["end"])
    return (None, data_point["end"])


def _fetch_company_facts(run_id: str, symbol: str = SYMBOL, cik: str = CIK) -> dict:
    """Fetch the full company-facts document once (every concept slices from it)."""
    return fetch_with_retry(
        COMPANY_FACTS_URL_TEMPLATE.format(cik=cik),
        {"User-Agent": USER_AGENT},
        {},
        f"sec_facts:{symbol}",
        run_id,
    )


def _data_points(facts: dict, namespace: str, tag: str, unit: str) -> list[dict]:
    """The raw data-point list for one tag/unit, or [] if the issuer never filed it.

    An absent key path is missing data (reported as zero rows), not an error.
    """
    return (
        facts.get("facts", {})
        .get(namespace, {})
        .get(tag, {})
        .get("units", {})
        .get(unit, [])
    )


def _select(facts: dict, concept: Concept) -> list[tuple[str, dict]]:
    """Walk the tag priority list, returning (supplying_tag, data_point) pairs.

    Fill-gap, never union-sum: a later tag contributes a point only for a fiscal
    period (``_period_key``) no earlier tag already supplied. Within a single tag,
    every surviving filing of a period is kept (insert-all) — coverage is decided
    per period, not per filing, so the later tags patch only true holes.
    """
    covered: set[tuple[str | None, str]] = set()
    selected: list[tuple[str, dict]] = []
    for tag in concept.tags:
        survivors = [
            dp
            for dp in _data_points(facts, concept.namespace, tag, concept.unit)
            if _survives(dp, concept.period_kind)
        ]
        fresh = [dp for dp in survivors if _period_key(dp, concept.period_kind) not in covered]
        selected.extend((tag, dp) for dp in fresh)
        covered |= {_period_key(dp, concept.period_kind) for dp in fresh}
    return selected


def _to_row(concept: Concept, tag: str, data_point: dict, symbol: str = SYMBOL) -> dict:
    """Map one SEC data point to a ``fundamentals`` row (1:1, no derived numbers)."""
    is_duration = concept.period_kind == DURATION
    return {
        "symbol": symbol,
        "concept": concept.name,
        "tag": tag,  # the tag that actually supplied this period (priority list).
        "unit": concept.unit,
        "is_split_adjustable": concept.is_split_adjustable,
        # Key on the actual period — never on fy/fp. Instant facts have no start.
        "period_start": data_point["start"] if is_duration else None,
        "period_end": data_point["end"],
        "value": data_point["val"],
        "accn": data_point["accn"],
        "form": data_point["form"],
        "filed": data_point["filed"],
        "raw_json": data_point,  # the original point, verbatim, for provenance/replay.
    }


def _row_count(client, concept: Concept, symbol: str = SYMBOL) -> int:
    """Current count of this concept's rows in ``fundamentals`` (for the delta)."""
    resp = (
        client.table("fundamentals")
        .select("id", count="exact")
        .eq("symbol", symbol)
        .eq("concept", concept.name)
        .execute()
    )
    return resp.count or 0


def ingest_concept(client, facts: dict, concept: Concept, symbol: str = SYMBOL) -> dict:
    """Filter -> map -> idempotent-upsert one concept; return its counts.

    ``mapped`` is rows offered to the upsert (after filter + fill-gap), ``inserted``
    is the true before/after row-count delta (0 on a re-run), ``total`` the row count
    now in the table for this concept.
    """
    selected = _select(facts, concept)
    rows = [_to_row(concept, tag, dp, symbol) for tag, dp in selected]
    by_tag = {tag: sum(1 for t, _ in selected if t == tag) for tag in concept.tags}

    before = _row_count(client, concept, symbol)
    if rows:
        client.table("fundamentals").upsert(
            rows,
            on_conflict=UPSERT_CONFLICT,
            ignore_duplicates=True,  # insert-once; NULLS NOT DISTINCT dedupes instants.
        ).execute()
    after = _row_count(client, concept, symbol)
    return {"mapped": len(rows), "inserted": after - before, "total": after, "by_tag": by_tag}


def _verify_pairing(symbol: str, cik: str, run_id: str) -> None:
    """Raise unless the SEC ticker map agrees ``symbol`` -> ``cik`` (Law 2 gate).

    A mispaired call would write another issuer's numbers under this symbol — the
    silent-corruption class Law 2 exists to kill — so the pairing is checked against
    the independent company_tickers.json map BEFORE any write. Imported lazily:
    ``analyst.cik`` imports this module for USER_AGENT, so a top-level import would
    be a cycle. A symbol the map lacks also raises — callers with a genuinely
    unmapped issuer pass ``verify_pairing=False`` explicitly, on their own head.
    """
    from analyst.cik import resolve_cik

    resolved = resolve_cik(symbol, run_id)
    if resolved is None:
        raise ValueError(
            f"sec_facts: {symbol!r} is not in the SEC ticker map; refusing to ingest "
            f"under an unverifiable CIK (pass verify_pairing=False to override)."
        )
    if resolved != cik:
        raise ValueError(
            f"sec_facts: symbol/CIK mispairing — {symbol!r} resolves to CIK {resolved}, "
            f"not {cik}. Refusing to write another issuer's numbers (Law 2)."
        )


def ingest_all(
    run_id: str | None = None,
    symbol: str = SYMBOL,
    cik: str = CIK,
    *,
    verify_pairing: bool = True,
) -> dict[str, dict]:
    """Fetch once -> ingest every concept for one issuer; return per-concept counts.

    The company-facts document is fetched a single time and every concept is sliced
    from it. A fetch outage is already in fetch_log (Law 7); it is surfaced and
    re-raised. Per-concept DB errors are NOT swallowed — they propagate.

    Args:
        run_id: fetch_log grouping id; a timestamped manual id is generated if omitted.
        symbol: issuer ticker written to ``fundamentals.symbol`` (default TSLA).
        cik: the issuer's 10-digit zero-padded CIK.
        verify_pairing: check symbol->cik against the SEC ticker map and REFUSE on
            mismatch before any write (default True; see :func:`_verify_pairing`).
    """
    run_id = run_id or _new_run_id("manual-sec_facts")
    if verify_pairing:
        _verify_pairing(symbol, cik, run_id)
    client = get_client()

    try:
        facts = _fetch_company_facts(run_id, symbol, cik)
    except FetchError as exc:
        # Already in fetch_log via the shared fetcher (Law 7); surface and re-raise.
        print(f"[sec_facts] {symbol}: company-facts unavailable — {exc}")
        raise

    results: dict[str, dict] = {}
    for concept in CONCEPTS:
        counts = ingest_concept(client, facts, concept, symbol)
        results[concept.name] = counts
        tag_note = ""
        if len(concept.tags) > 1:
            tag_note = "  [" + ", ".join(f"{t}={n}" for t, n in counts["by_tag"].items()) + "]"
        print(
            f"[sec_facts] {symbol} {concept.name:<20} "
            f"{counts['mapped']:>3} mapped, "
            f"{counts['inserted']:>3} inserted, "
            f"{counts['total']:>3} in table{tag_note}"
        )

    total_inserted = sum(c["inserted"] for c in results.values())
    print(
        f"[sec_facts] {symbol}: {total_inserted} row(s) inserted this run "
        f"across {len(CONCEPTS)} concepts."
    )
    return results


if __name__ == "__main__":
    ingest_all()
