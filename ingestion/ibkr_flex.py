"""Argus ingestion — IBKR Flex Web Service fetcher (blueprint §6 / §8 / §2 item 4).

Pulls the daily portfolio from IBKR's Flex Web Service (a two-step, XML-only API)
and stores three sections into three tables:

    OpenPositions    -> positions_snapshot   (date, symbol, qty, cost_basis, market_value)
    Trades           -> transactions          (exec_time, symbol, side, qty, price, fees,
                                                trade_type [auto-classified])
    CashTransactions -> contributions         (date, amount) — DCA deposits only (Law 5)

Two-step protocol:
    1. SendRequest  GET .../FlexStatementService.SendRequest?t=<token>&q=<query_id>&v=3
       -> <FlexStatementResponse><Status>Success</Status><ReferenceCode>..</><Url>..</></>
    2. GetStatement GET <Url>?t=<token>&q=<ReferenceCode>&v=3
       -> <FlexQueryResponse>..</> (the statement may still be generating; we poll).

All HTTP goes through :func:`shared.fetcher_base.fetch_with_retry` with
``parse="text"`` (Flex is XML, not JSON); parsing uses the stdlib
``xml.etree.ElementTree``. The Flex ``t`` token rides in the query string (no header
auth) but is redacted from any error the shared fetcher logs/raises (§13). One
``fetch_log`` row is written per section per run (Law 7); a transport outage logs
all three sections as ``unavailable`` and re-raises, because a Flex failure blinds
the journal (§12 critical alert).

Trade classification (§4 / §2 item 3): by quantity proximity to the sleeve size,
read from ``config.sleeve_shares`` at runtime (never hardcoded — Corporate Actions
auto-adjust it on splits, §4). Sleeve-sized legs -> round_trip_*; DCA-sized buys ->
dca_*; the rest -> unclassified. /override always wins later (§4). Same-day
sell->rebuy *pairing* into ``round_trips`` is Phase 2 (journal), not done here.

DEFERRED (PHASE0-TODO.md; not in the §4 schema, so out of scope for these 6 files):
  • transactions.ext_id + unique(ext_id): without it, re-running a Flex pull that
    re-covers a window re-inserts duplicate transactions/positions/contributions.
    These writes are plain inserts (not idempotent) until ext_id lands. Run once/day.
  • contributions.currency: Flex amounts are assumed to be in the account base
    currency (USD). JD<->USD normalization is deferred.

Run:  python -m ingestion.ibkr_flex   (or: python ingestion/ibkr_flex.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime

from shared.db import get_client
from shared.exceptions import FetchError
from shared.fetch_logger import write_fetch_log
from shared.fetcher_base import fetch_with_retry

FLEX_SERVICE_BASE = (
    "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService"
)
FLEX_SEND_REQUEST_URL = f"{FLEX_SERVICE_BASE}.SendRequest"
_FLEX_VERSION = "3"

# IBKR may still be generating the statement right after SendRequest (error 1019);
# poll a few times before giving up (§12 reliability is otherwise the shared fetcher's).
_GENERATION_WAIT_SECONDS = 5.0
_GENERATION_MAX_TRIES = 5

# Quantity-proximity classifier thresholds (§4 / §2 item 3).
_SLEEVE_PROXIMITY = 0.8  # a round-trip leg is qty >= 0.8 * sleeve_shares
_DCA_MAX_QTY = 2.0       # DCA buys are ~0.6-2 shares
_DEFAULT_SLEEVE_SHARES = 17.0  # fallback only when config.sleeve_shares is unseeded (§8)

# fetch_log source labels — one per section per run (Law 7).
_SECTIONS = ("ibkr_flex:positions", "ibkr_flex:trades", "ibkr_flex:cash")


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _num(value: str | None) -> float | None:
    """Parse a Flex numeric attribute to float; '' / None / unparseable -> None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_flex_dt(value: str | None) -> datetime | None:
    """Parse the assorted IBKR Flex date/time formats into a ``datetime``.

    Flex emits dates in formats that vary with the query config: 'YYYYMMDD',
    'YYYYMMDD;HHMMSS', 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS', or full ISO 8601
    (optionally with 'Z' / a tz offset). Unknown shapes yield None — a null cell,
    never a crash that drops the whole batch (Law 7).
    """
    if not value:
        return None
    raw = value.strip().replace(";", " ")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y%m%d %H%M%S", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _flex_date(value: str | None) -> str | None:
    """Normalize a Flex date/datetime attribute to an ISO date 'YYYY-MM-DD'."""
    parsed = _parse_flex_dt(value)
    return parsed.date().isoformat() if parsed else None


def _flex_datetime(value: str | None) -> str | None:
    """Normalize a Flex dateTime attribute to ISO 8601, or None."""
    parsed = _parse_flex_dt(value)
    return parsed.isoformat() if parsed else None


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _load_sleeve_shares(run_id: str) -> float:
    """Read ``config.sleeve_shares`` at runtime; fall back to the §8 default of 17.

    Never hardcoded as a constant in trade logic: Corporate Actions auto-adjust the
    share count on splits and write a new ``config`` row (§4). Two fallback paths,
    deliberately distinguished:

    * config *unseeded* (no row yet) — a legitimate Phase-0 state; the documented
      default of 17 applies silently.
    * config read *raises* — a real failure that must not silently change trade
      classification. It is logged to ``fetch_log`` first (Law 7: surface it, never
      swallow) so a config outage that altered classification leaves a DB trace.
    """
    start = time.monotonic()
    try:
        resp = (
            get_client()
            .table("config")
            .select("value")
            .eq("key", "sleeve_shares")
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 — surface, never swallow (Law 7)
        write_fetch_log("ibkr_flex:config", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[ibkr_flex] config.sleeve_shares read FAILED ({exc}); using default {_DEFAULT_SLEEVE_SHARES}.")
        return _DEFAULT_SLEEVE_SHARES

    if resp.data:
        return float(resp.data[0]["value"])
    print(f"[ibkr_flex] config.sleeve_shares unseeded; using Phase-0 default {_DEFAULT_SLEEVE_SHARES}.")
    return _DEFAULT_SLEEVE_SHARES


def _classify(side: str, magnitude: float | None, sleeve_shares: float) -> str:
    """Auto-assign ``transactions.trade_type`` by quantity proximity (§4 / §2 item 3).

    Sleeve-sized (|qty| >= 0.8 * sleeve_shares): a sell -> 'round_trip_sell', a buy ->
    'round_trip_rebuy'. DCA-sized (|qty| <= 2): a buy -> 'dca_buy', a sell -> 'dca_sell'.
    Anything in between (or a missing/zero qty) -> 'unclassified'. /override wins later.
    """
    if magnitude is None or magnitude <= 0:
        return "unclassified"
    if sleeve_shares > 0 and magnitude >= _SLEEVE_PROXIMITY * sleeve_shares:
        return "round_trip_sell" if side == "sell" else "round_trip_rebuy"
    if magnitude <= _DCA_MAX_QTY:
        return "dca_buy" if side == "buy" else "dca_sell"
    return "unclassified"


# --------------------------------------------------------------------------- #
# Retrieval (two-step protocol)
# --------------------------------------------------------------------------- #
def _retrieve_statement(token: str, query_id: str, run_id: str) -> ET.Element:
    """Run SendRequest -> GetStatement and return the ``<FlexStatement>`` element.

    Raises:
        FetchError: on transport outage (from the shared fetcher), a Fail status,
            or a statement that never finishes generating.
    """
    send_xml = fetch_with_retry(
        FLEX_SEND_REQUEST_URL,
        {},
        {"t": token, "q": query_id, "v": _FLEX_VERSION},
        "ibkr_flex:send",
        run_id,
        parse="text",
    )
    send_root = ET.fromstring(send_xml)
    if send_root.findtext("Status") != "Success":
        detail = (
            send_root.findtext("ErrorMessage")
            or send_root.findtext("ErrorCode")
            or "unknown error"
        )
        raise FetchError("ibkr_flex:send", f"SendRequest failed: {detail}")

    reference_code = send_root.findtext("ReferenceCode")
    statement_url = send_root.findtext("Url")
    if not reference_code or not statement_url:
        raise FetchError("ibkr_flex:send", "SendRequest missing ReferenceCode/Url")

    for attempt in range(1, _GENERATION_MAX_TRIES + 1):
        statement_xml = fetch_with_retry(
            statement_url,
            {},
            {"t": token, "q": reference_code, "v": _FLEX_VERSION},
            "ibkr_flex:get",
            run_id,
            parse="text",
        )
        root = ET.fromstring(statement_xml)
        if root.tag == "FlexQueryResponse":
            statement = root.find(".//FlexStatement")
            if statement is None:
                raise FetchError("ibkr_flex:get", "statement has no FlexStatement element")
            return statement

        # Otherwise it's a FlexStatementResponse carrying a status (often "in progress").
        code = root.findtext("ErrorCode") or ""
        message = root.findtext("ErrorMessage") or ""
        still_generating = code == "1019" or "generat" in message.lower()
        if still_generating and attempt < _GENERATION_MAX_TRIES:
            time.sleep(_GENERATION_WAIT_SECONDS)
            continue
        raise FetchError(
            "ibkr_flex:get", f"GetStatement failed: {message or code or 'unknown error'}"
        )

    raise FetchError("ibkr_flex:get", "statement not ready after polling")


def _load_known_symbols() -> set[str]:
    """Return the set of symbols in ``instruments`` (the tracked FK universe)."""
    resp = get_client().table("instruments").select("symbol").execute()
    return {row["symbol"] for row in resp.data}


# --------------------------------------------------------------------------- #
# Section stores (one fetch_log row each)
# --------------------------------------------------------------------------- #
def _store_positions(statement: ET.Element, run_id: str, known: set[str]) -> bool:
    """Store OpenPositions -> ``positions_snapshot``; log the section outcome. True=ok."""
    start = time.monotonic()
    try:
        statement_date = statement.get("toDate")
        rows, skipped, undated = [], set(), 0
        for pos in statement.findall(".//OpenPositions/OpenPosition"):
            symbol = pos.get("symbol")
            if symbol not in known:
                skipped.add(symbol)
                continue
            row_date = _flex_date(pos.get("reportDate") or statement_date)
            if row_date is None:
                # positions_snapshot.date is NOT NULL: skip one undatable row rather
                # than letting the batch insert fail and blind the whole section.
                undated += 1
                continue
            rows.append(
                {
                    "date": row_date,
                    "symbol": symbol,
                    "qty": _num(pos.get("position")),
                    "cost_basis": _num(pos.get("costBasisMoney")),
                    "market_value": _num(pos.get("positionValue")),
                }
            )
        if rows:
            get_client().table("positions_snapshot").insert(rows).execute()
        if skipped:
            print(f"[ibkr_flex] positions: skipped untracked symbol(s): {sorted(skipped)}")
        if undated:
            print(f"[ibkr_flex] positions: skipped {undated} row(s) with unresolved date.")
        write_fetch_log("ibkr_flex:positions", run_id, "success", _elapsed_ms(start))
        print(f"[ibkr_flex] positions: stored {len(rows)} row(s).")
        return True
    except Exception as exc:  # noqa: BLE001 — surface the failure, never swallow it
        write_fetch_log("ibkr_flex:positions", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[ibkr_flex] positions: FAILED — {exc}")
        return False


def _store_trades(
    statement: ET.Element, run_id: str, known: set[str], sleeve_shares: float
) -> bool:
    """Store Trades -> ``transactions`` (auto-classified); log the section outcome."""
    start = time.monotonic()
    try:
        rows, skipped = [], set()
        for trade in statement.findall(".//Trades/Trade"):
            symbol = trade.get("symbol")
            if symbol not in known:
                skipped.add(symbol)
                continue
            side = (trade.get("buySell") or "").lower()
            if side not in ("buy", "sell"):
                continue  # skip non buy/sell rows (would violate the side CHECK)
            qty = _num(trade.get("quantity"))
            magnitude = abs(qty) if qty is not None else None  # IBKR sells are negative
            commission = _num(trade.get("ibCommission"))
            rows.append(
                {
                    "exec_time": _flex_datetime(trade.get("dateTime")),
                    "symbol": symbol,
                    "side": side,
                    # qty stored as positive magnitude; `side` carries direction.
                    "qty": magnitude,
                    "price": _num(trade.get("tradePrice")),
                    # fees stored as a positive cost (abs of IBKR's signed commission).
                    "fees": abs(commission) if commission is not None else None,
                    "trade_type": _classify(side, magnitude, sleeve_shares),
                }
            )
        if rows:
            get_client().table("transactions").insert(rows).execute()
        if skipped:
            print(f"[ibkr_flex] trades: skipped untracked symbol(s): {sorted(skipped)}")
        write_fetch_log("ibkr_flex:trades", run_id, "success", _elapsed_ms(start))
        print(f"[ibkr_flex] trades: stored {len(rows)} transaction(s).")
        return True
    except Exception as exc:  # noqa: BLE001
        write_fetch_log("ibkr_flex:trades", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[ibkr_flex] trades: FAILED — {exc}")
        return False


def _store_cash(statement: ET.Element, run_id: str) -> bool:
    """Store CashTransactions -> ``contributions`` (DCA deposits only, Law 5)."""
    start = time.monotonic()
    try:
        rows, undated = [], 0
        for cash in statement.findall(".//CashTransactions/CashTransaction"):
            ctype = (cash.get("type") or "").lower()
            amount = _num(cash.get("amount"))
            # DCA deposits only: positive "Deposits/Withdrawals". Dividends, interest,
            # fees and taxes are not contributions (they never feed the core track).
            if "deposit" not in ctype or amount is None or amount <= 0:
                continue
            row_date = _flex_date(
                cash.get("settleDate") or cash.get("dateTime") or cash.get("reportDate")
            )
            if row_date is None:
                # contributions.date is NOT NULL: skip one undatable deposit rather
                # than letting the batch insert fail and blind the whole section.
                undated += 1
                continue
            rows.append({"date": row_date, "amount": amount})
        if rows:
            get_client().table("contributions").insert(rows).execute()
        if undated:
            print(f"[ibkr_flex] cash: skipped {undated} deposit(s) with unresolved date.")
        write_fetch_log("ibkr_flex:cash", run_id, "success", _elapsed_ms(start))
        print(f"[ibkr_flex] cash: stored {len(rows)} contribution(s).")
        return True
    except Exception as exc:  # noqa: BLE001
        write_fetch_log("ibkr_flex:cash", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[ibkr_flex] cash: FAILED — {exc}")
        return False


def _elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for fetch_log)."""
    return int((time.monotonic() - start) * 1000)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def fetch_flex(run_id: str) -> None:
    """Fetch the daily IBKR Flex statement and store its three sections (§6 / §8).

    Args:
        run_id: Run identifier, logged to ``fetch_log`` to group this run's fetches.

    Raises:
        FetchError: on a transport outage (each section logged ``unavailable`` first,
            then re-raised — a Flex failure blinds the journal, §12), or if any
            section fails to store after a successful fetch.
    """
    token = os.environ.get("IBKR_FLEX_TOKEN")
    query_id = os.environ.get("IBKR_FLEX_QUERY_ID")
    if not token or not query_id:
        raise RuntimeError("Missing IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID (see .env.example).")

    try:
        statement = _retrieve_statement(token, query_id, run_id)
    except FetchError as exc:
        # Flex down = journal goes blind (§12). Surface every section, then fail loud.
        for section in _SECTIONS:
            write_fetch_log(section, run_id, "unavailable", 0, str(exc))
        print(f"[ibkr_flex] statement unavailable — {exc}")
        raise

    known = _load_known_symbols()
    sleeve_shares = _load_sleeve_shares(run_id)

    results = [
        _store_positions(statement, run_id, known),
        _store_trades(statement, run_id, known, sleeve_shares),
        _store_cash(statement, run_id),
    ]
    if not all(results):
        raise FetchError("ibkr_flex", "one or more sections failed to store; see fetch_log")


if __name__ == "__main__":
    import uuid

    manual_run_id = f"manual-flex-{uuid.uuid4().hex[:12]}"
    fetch_flex(manual_run_id)
