"""Argus journal — the sleeve-entry writer (§8 / §2.2b): derive, show, confirm, freeze.

The ONE deliberate human action that arms the journal. At sleeve entry the registered
unit is

    sleeve_shares = floor(sleeve_pct × live_portfolio_value ÷ sleeve-symbol price)

frozen through the 50-trade verdict and re-derived only at a phase gate (blueprint §8).
This module computes that derivation from STORED rows only (Law 2 — every input is a
DB row or a config row, each printed with its provenance), shows the full math, and
writes the single ``config.sleeve_shares`` key ONLY after the operator types the exact
confirmation word. Design constraints, all deliberate:

  * A guided CLI, not a Telegram command and not a scheduled job (L8): it REFUSES to
    run without an interactive terminal, so no workflow can ever register a sleeve.
  * Information, never instruction (L1): this tool never suggests running itself and
    never comments on whether NOW is a good moment to open a sleeve — it renders
    mechanics and refusals only. Whether and when to run it is Omar's alone.
  * Fee-dominance floor: ``config.min_sleeve_shares`` (seeded by
    ``python -m journal.seed_sleeve_floor N`` — a pre-registered guard, no in-code
    default) — below it the $-bracket structurally loses to fees, so the writer
    refuses rather than registering an unworkable unit.
  * Fail-loud preflight: already-registered sleeve, missing config, stale positions
    snapshot or stale price (> ``_MAX_INPUT_AGE_DAYS``), or underivable cash each
    REFUSE with the reason — nothing is ever written on a refusal path.
  * Cash is DERIVED from stored rows (contributions − buy cost − fees + sell
    proceeds): the spine stores no broker cash-balance line. When the earliest fill
    predates the earliest contribution (the pre-widen Flex hole), the derivation is
    provably missing a deposit and the writer says so — and refuses, because a
    portfolio value known to be understated would register a wrong unit (L6).

No fetch_log rows: this is an interactive operator tool, not a wrapped fetcher —
failures print and exit non-zero in front of the human who ran it (L7's "surfaced"
is the terminal itself here).

Run:  python -m journal.sleeve_open            (interactive; writes only on 'REGISTER')
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import math
import sys
from datetime import date, datetime, timezone

from shared.db import get_client

# Inputs older than this refuse the registration. 4 calendar days is the weekend-safe
# envelope of §12's "2 trading days" staleness line — a Friday snapshot read on Monday
# (3 calendar days) passes; anything staler than a long weekend does not.
_MAX_INPUT_AGE_DAYS = 4

_CONFIRM_WORD = "REGISTER"


# --------------------------------------------------------------------------- #
# Pure core (unit-tested; no DB, no terminal)
# --------------------------------------------------------------------------- #
def derive_sleeve_shares(sleeve_pct: float, portfolio_value: float, price: float) -> int:
    """floor(sleeve_pct × portfolio_value ÷ price) — the §8 registered unit."""
    if not (0 < sleeve_pct <= 1):
        raise ValueError(f"sleeve_pct must be in (0, 1], got {sleeve_pct!r}")
    if portfolio_value <= 0:
        raise ValueError(f"portfolio_value must be positive, got {portfolio_value!r}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price!r}")
    return int(math.floor(sleeve_pct * portfolio_value / price))


def derive_cash(contributions: list[dict], transactions: list[dict]) -> dict:
    """Cash derived from stored rows: deposits − buy cost − fees + sell proceeds.

    Returns {cash, deposits, buy_cost, sell_proceeds, fees, caveats}. The caveat that
    MATTERS (and blocks registration) is ``missing_deposit``: a fill exists that no
    stored contribution could have funded — the earliest transaction predates the
    earliest contribution — so the derived cash is provably understated (the
    pre-widen Flex window hole). qty is stored as positive magnitude with ``side``
    carrying direction; fees are stored as positive costs.
    """
    deposits = sum(float(r["amount"]) for r in contributions if r.get("amount") is not None)
    buy_cost = sell_proceeds = fees = 0.0
    for t in transactions:
        qty, price = t.get("qty"), t.get("price")
        if qty is not None and price is not None:
            value = float(qty) * float(price)
            if t.get("side") == "buy":
                buy_cost += value
            elif t.get("side") == "sell":
                sell_proceeds += value
        if t.get("fees") is not None:
            fees += float(t["fees"])

    caveats: list[str] = []
    if transactions and not contributions:
        caveats.append("missing_deposit")
    elif transactions and contributions:
        first_txn = min(
            (t.get("exec_time") or t.get("created_at") or "") for t in transactions
        )[:10]
        first_contrib = min(str(r.get("date") or "") for r in contributions)
        if first_txn and first_contrib and first_txn < first_contrib:
            caveats.append("missing_deposit")

    return {
        "cash": deposits - buy_cost - fees + sell_proceeds,
        "deposits": deposits,
        "buy_cost": buy_cost,
        "sell_proceeds": sell_proceeds,
        "fees": fees,
        "caveats": caveats,
    }


def preflight_blockers(
    *,
    sleeve_shares_row,
    sleeve_pct,
    min_floor,
    positions_date: str | None,
    price_date: str | None,
    cash_caveats: list[str],
    today: date,
) -> list[str]:
    """Every reason registration must refuse (empty list = clear to derive). Pure."""
    blockers: list[str] = []
    if sleeve_shares_row is not None:
        blockers.append(
            f"config.sleeve_shares is already registered ({sleeve_shares_row!r}) — the unit "
            f"is frozen through the 50-trade verdict and re-derived only at a phase gate "
            f"(§8). Delete the key deliberately (single-key) before re-deriving."
        )
    if not isinstance(sleeve_pct, (int, float)) or not (0 < float(sleeve_pct) <= 1):
        blockers.append(f"config.sleeve_pct missing or invalid ({sleeve_pct!r}) — seed it first.")
    if not isinstance(min_floor, (int, float)) or float(min_floor) < 1:
        blockers.append(
            f"config.min_sleeve_shares missing or invalid ({min_floor!r}) — the fee-dominance "
            f"floor is a pre-registered guard: seed it with "
            f"`python -m journal.seed_sleeve_floor <shares>` (no in-code default, L6)."
        )
    for label, d in (("positions_snapshot", positions_date), ("sleeve price", price_date)):
        if not d:
            blockers.append(f"no {label} row on record — cannot value the portfolio.")
            continue
        age = (today - date.fromisoformat(str(d)[:10])).days
        if age > _MAX_INPUT_AGE_DAYS:
            blockers.append(
                f"{label} is {age} days old ({d}) — staler than {_MAX_INPUT_AGE_DAYS} days; "
                f"registration needs fresh inputs (§12). Re-pull first."
            )
    if "missing_deposit" in cash_caveats:
        blockers.append(
            "derived cash is provably missing a deposit (a fill predates every stored "
            "contribution — the pre-widen Flex window hole). Registering on an understated "
            "portfolio value would freeze a wrong unit (L6). Complete the contributions "
            "backfill first (widen the Flex query window, re-pull, restore)."
        )
    return blockers


# --------------------------------------------------------------------------- #
# Thin CLI (reads the spine, prints the math, writes on explicit confirm)
# --------------------------------------------------------------------------- #
def _config_value(client, key: str):
    rows = client.table("config").select("value").eq("key", key).limit(1).execute().data or []
    return rows[0]["value"] if rows else None


def main() -> int:
    if not sys.stdin.isatty():
        print(
            "[sleeve_open] REFUSED: no interactive terminal. Registering the sleeve is a "
            "deliberate human action — this tool never runs from a job or a pipe."
        )
        return 1

    client = get_client()
    today = datetime.now(timezone.utc).date()

    sleeve_symbol = _config_value(client, "sleeve_symbol")
    if not isinstance(sleeve_symbol, str) or not sleeve_symbol.strip():
        print("[sleeve_open] REFUSED: config.sleeve_symbol missing/invalid — never guessed (L6).")
        return 1
    sleeve_symbol = sleeve_symbol.strip()

    sleeve_shares_row = _config_value(client, "sleeve_shares")
    sleeve_pct = _config_value(client, "sleeve_pct")
    min_floor = _config_value(client, "min_sleeve_shares")

    pos_rows = (
        client.table("positions_snapshot").select("date,symbol,qty,market_value")
        .order("date", desc=True).limit(20).execute().data or []
    )
    positions_date = pos_rows[0]["date"] if pos_rows else None
    positions = [r for r in pos_rows if r["date"] == positions_date]

    price_rows = (
        client.table("prices_eod").select("close,date").eq("symbol", sleeve_symbol)
        .order("date", desc=True).limit(1).execute().data or []
    )
    price = float(price_rows[0]["close"]) if price_rows and price_rows[0].get("close") is not None else None
    price_date = price_rows[0]["date"] if price_rows else None

    contributions = client.table("contributions").select("date,amount").order("date").execute().data or []
    transactions = (
        client.table("transactions").select("exec_time,created_at,side,qty,price,fees")
        .order("id").execute().data or []
    )
    cash = derive_cash(contributions, transactions)

    blockers = preflight_blockers(
        sleeve_shares_row=sleeve_shares_row,
        sleeve_pct=sleeve_pct,
        min_floor=min_floor,
        positions_date=positions_date,
        price_date=price_date if price is not None else None,
        cash_caveats=cash["caveats"],
        today=today,
    )
    if blockers:
        print("[sleeve_open] REFUSED — resolve, then re-run:")
        for b in blockers:
            print(f"  • {b}")
        return 1

    equity_value = sum(float(r["market_value"]) for r in positions if r.get("market_value") is not None)
    portfolio_value = equity_value + cash["cash"]

    print("SLEEVE REGISTRATION — every input is a stored row; nothing here is a suggestion.")
    print(f"  positions_snapshot {positions_date}: "
          + "; ".join(f"{r['symbol']} qty {r['qty']} mv {r['market_value']}" for r in positions))
    print(f"  equity market value:        {equity_value:,.2f}")
    print(f"  cash (derived from rows):   {cash['cash']:,.2f}  "
          f"= deposits {cash['deposits']:,.2f} − buys {cash['buy_cost']:,.2f} "
          f"− fees {cash['fees']:,.2f} + sells {cash['sell_proceeds']:,.2f}")
    print(f"  live portfolio value:       {portfolio_value:,.2f}")
    print(f"  sleeve_pct (config):        {float(sleeve_pct):.0%}")
    print(f"  {sleeve_symbol} close (prices_eod {price_date}): {price:,.2f}")

    shares = derive_sleeve_shares(float(sleeve_pct), portfolio_value, price)
    print(f"  derivation: floor({float(sleeve_pct)} × {portfolio_value:,.2f} ÷ {price:,.2f}) "
          f"= floor({float(sleeve_pct) * portfolio_value / price:,.2f}) = {shares} share(s)")
    print(f"  fee-dominance floor (config.min_sleeve_shares): {int(min_floor)}")

    if shares < int(min_floor):
        print(f"[sleeve_open] REFUSED: derived unit {shares} is below the registered floor "
              f"{int(min_floor)} — at that size the bracket structurally loses to fees. "
              f"Nothing written.")
        return 1

    print(f"\nRegistering freezes sleeve_shares = {shares} as the §8 unit through the 50-trade "
          f"verdict (re-derived only at a phase gate); the Flex classifier will treat legs "
          f"≥ {0.8 * shares:g} shares as round-trip-sized from the next pull.")
    answer = input(f"Type {_CONFIRM_WORD} to write config.sleeve_shares = {shares} "
                   f"(anything else aborts): ").strip()
    if answer != _CONFIRM_WORD:
        print("[sleeve_open] aborted — nothing written.")
        return 1

    client.table("config").upsert(
        [{"key": "sleeve_shares", "value": shares}], on_conflict="key"
    ).execute()
    stored = _config_value(client, "sleeve_shares")
    ok = stored == shares
    print(f"[sleeve_open] config.sleeve_shares = {shares} written + read-back "
          f"{'verified' if ok else f'MISMATCH (!) — stored {stored!r}'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
