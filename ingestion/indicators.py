"""Argus ingestion — local technical indicators (blueprint §5 / §6).

Indicators are computed LOCALLY from ``prices_eod`` via ``pandas_ta`` — zero API cost
(§5) — and written to the ``indicators`` table as a per-(symbol, date, name) time series
so the digest can read the latest values and so they can be validated against
TradingView at build time.

Computed per symbol: SMA 50, SMA 200, RSI 14, MACD line, MACD signal, MACD histogram.

Young-ticker suppression (§4): a row is written only where the value is a real number.
An indicator whose minimum period exceeds the symbol's history is skipped entirely (no
rows), and any per-date warmup NaN is skipped — absence encodes suppression; we never
store a NaN. SPCX (listed 2026-06-12, ~2 sessions) therefore gets no indicator rows yet.

DEVIATION FROM THE TASK BRIEF (applied schema is truth): the brief says to read the
``indicators.name`` CHECK constraint and only write permitted values — but the applied
migration deliberately puts NO CHECK on ``name`` (it is an OPEN set so new indicators
need no DDL). The canonical names below follow the blueprint §4 examples
(sma50/sma200/rsi14/macd…) and ``digest/bundle.py`` reads them back name-agnostically.

Both the full-table price read and the indicator upserts page past PostgREST's 1000-row
cap so neither a long history nor a large back-computation is silently truncated (Law 7).

Run:  python -m ingestion.indicators   (or: python ingestion/indicators.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import time

import pandas as pd
import pandas_ta as ta

from shared.db import get_client
from shared.fetch_logger import write_fetch_log

# Canonical indicator names (open set — no DB CHECK; see module docstring) and the
# minimum number of sessions required before each yields a usable value.
_MIN_PERIODS: dict[str, int] = {
    "sma50": 50,
    "sma200": 200,
    "rsi14": 14,
    "macd": 26,
    "macd_signal": 26,
    "macd_hist": 26,
}

_PAGE = 1000          # PostgREST page size for reads
_UPSERT_CHUNK = 500   # rows per upsert request


def _elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for fetch_log)."""
    return int((time.monotonic() - start) * 1000)


def _chunks(seq: list, size: int):
    """Yield successive ``size``-length slices of ``seq``."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _read_prices(symbol: str) -> list[dict]:
    """Read a symbol's full (date, close) history ascending, paging past the 1000 cap."""
    client = get_client()
    rows: list[dict] = []
    start = 0
    while True:
        batch = (
            client.table("prices_eod")
            .select("date,close")
            .eq("symbol", symbol)
            .order("date")
            .range(start, start + _PAGE - 1)
            .execute()
            .data
            or []
        )
        rows.extend(batch)
        if len(batch) < _PAGE:
            break
        start += _PAGE
    return rows


def _macd_columns(macd: pd.DataFrame) -> dict[str, pd.Series]:
    """Map a ``ta.macd`` DataFrame to canonical names by column prefix (order-proof).

    pandas_ta yields ``MACD_12_26_9`` (line), ``MACDh_12_26_9`` (histogram),
    ``MACDs_12_26_9`` (signal); we select by prefix rather than position.
    """
    out: dict[str, pd.Series] = {}
    for name, prefix in (("macd", "MACD_"), ("macd_hist", "MACDh_"), ("macd_signal", "MACDs_")):
        col = next((c for c in macd.columns if c.startswith(prefix)), None)
        if col is not None:
            out[name] = macd[col]
    return out


def compute_indicators(run_id: str) -> None:
    """Compute indicators for every tracked symbol and upsert them into ``indicators``.

    Args:
        run_id: Run identifier, logged to ``fetch_log`` (source='indicators').

    Reads all of ``prices_eod`` per symbol, computes the six indicators with pandas_ta,
    and upserts one row per (symbol, date, name) where the value is real — suppressing
    young tickers and warmup NaNs by omission (§4). Re-running updates existing values
    (on_conflict='symbol,date,name'). The whole computation is timed; success/failure is
    logged to fetch_log and a failure is re-raised, never swallowed (Law 7).
    """
    start = time.monotonic()
    try:
        client = get_client()
        symbols = [
            row["symbol"]
            for row in (client.table("instruments").select("symbol").execute().data or [])
        ]
        floor = min(_MIN_PERIODS.values())  # below this, nothing is computable
        total = 0

        for symbol in symbols:
            prices = _read_prices(symbol)
            if len(prices) < floor:
                print(f"[indicators] {symbol}: {len(prices)} session(s) < {floor}; all suppressed.")
                continue

            frame = pd.DataFrame(prices)
            frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
            frame = frame.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
            sessions = len(frame)

            series: dict[str, pd.Series] = {}
            if sessions >= _MIN_PERIODS["sma50"]:
                series["sma50"] = ta.sma(frame["close"], length=50)
            if sessions >= _MIN_PERIODS["sma200"]:
                series["sma200"] = ta.sma(frame["close"], length=200)
            if sessions >= _MIN_PERIODS["rsi14"]:
                series["rsi14"] = ta.rsi(frame["close"], length=14)
            if sessions >= _MIN_PERIODS["macd"]:
                macd = ta.macd(frame["close"], fast=12, slow=26, signal=9)
                if macd is not None and not macd.empty:
                    series.update(_macd_columns(macd))

            rows: list[dict] = []
            for name, values in series.items():
                for date_str, value in zip(frame["date"], values):
                    if value is None or pd.isna(value):
                        continue  # warmup NaN — write nothing rather than a NaN (§4)
                    rows.append(
                        {"symbol": symbol, "date": date_str, "name": name, "value": float(value)}
                    )

            for chunk in _chunks(rows, _UPSERT_CHUNK):
                client.table("indicators").upsert(chunk, on_conflict="symbol,date,name").execute()
            total += len(rows)
            print(f"[indicators] {symbol}: upserted {len(rows)} row(s) over {sessions} session(s).")

        write_fetch_log("indicators", run_id, "success", _elapsed_ms(start))
        print(f"[indicators] done: {total} row(s) across {len(symbols)} symbol(s).")
    except Exception as exc:  # noqa: BLE001 — surface, log, never swallow (Law 7)
        write_fetch_log("indicators", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[indicators] FAILED — {exc}")
        raise


if __name__ == "__main__":
    import uuid

    compute_indicators(f"manual-indicators-{uuid.uuid4().hex[:12]}")
