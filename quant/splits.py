"""Argus quant — split-adjustment read layer over the fundamentals stack.

EDGAR's history is a patchwork of share bases: a filing reports share counts on the
basis in effect at its FILED date, and comparatives in later filings arrive already
restated to the newer basis. TSLA's raw ``shares_diluted`` therefore jumps 166M ->
853M -> 2,798M across filings — split artifacts, not dilution. This layer multiplies
each stored value by the product of the ratios of every split whose ``effective_date``
is AFTER the row's ``filed`` date (the universally correct rule — keyed on the filing
date, never on ``period_end``), putting the whole history on today's basis.

Read-time only (Law 2): nothing here is stored. Every adjusted value is
``value_raw × factor`` where both inputs are stored rows (``fundamentals_latest`` ×
``corporate_actions``); the returned records keep ``value_raw``, the factor applied,
and the row's full provenance (``period_end`` / ``accn`` / ``form`` / ``filed``).

Only ``action_type == 'split'`` rows contribute to the factor — a future dividend or
spinoff row in ``corporate_actions`` must never silently multiply share counts. A
split row with a missing/non-positive ratio fails loud (Law 7): silently skipping one
would put every per-share figure on a wrong basis (an L6-class corruption), which is
strictly worse than a crash.

Run:  python -m quant.splits   (prints TSLA's full adjusted shares_diluted series —
      the regression probe: smooth ~1.9B (2015) -> ~3.5B (2025), no ×5/×15 jumps)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import date

from shared.db import get_client


def _to_date(value: object) -> date:
    """Parse an ISO date (or pass a date through); anything else fails loud."""
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unparseable date {value!r} in split-adjustment input") from exc


def factor_from_splits(splits: list[dict], filed: object) -> float:
    """Pure core: product of ratios of splits with ``effective_date`` > ``filed``.

    ``splits`` rows need ``effective_date`` and ``ratio`` (new-shares-per-old-share).
    A row with a missing or non-positive ratio raises — see module docstring.
    """
    filed_date = _to_date(filed)
    factor = 1.0
    for row in splits:
        ratio = row.get("ratio")
        try:
            ratio = float(ratio)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            ratio = None
        if ratio is None or ratio <= 0:
            raise ValueError(
                f"corporate_actions split row has invalid ratio {row.get('ratio')!r} "
                f"(effective {row.get('effective_date')!r}) — refusing to skip it silently"
            )
        if _to_date(row.get("effective_date")) > filed_date:
            factor *= ratio
    return factor


def load_splits(symbol: str, client=None) -> list[dict]:
    """The symbol's split rows from ``corporate_actions``, ordered by effective_date."""
    client = client or get_client()
    return (
        client.table("corporate_actions")
        .select("effective_date,ratio")
        .eq("symbol", symbol)
        .eq("action_type", "split")
        .order("effective_date")
        .execute()
        .data
        or []
    )


def split_factor(symbol: str, filed_date: object, client=None) -> float:
    """Split factor for one (symbol, filed_date): product of later splits' ratios.

    Convenience single-row form; series readers use :func:`load_splits` once +
    :func:`factor_from_splits` per row instead of one query per row.
    """
    return factor_from_splits(load_splits(symbol, client), filed_date)


def read_concept(symbol: str, concept: str, client=None) -> list[dict]:
    """One concept's annual series from ``fundamentals_latest``, split-adjusted.

    Returns records ordered by ``period_end``, each carrying:
      value        — value_raw × split_factor where ``is_split_adjustable``, else
                     value_raw unchanged (dollar flows have no share basis).
      value_raw    — the stored value, verbatim.
      split_factor — the factor actually applied (1.0 when not adjustable).
      provenance   — period_start/period_end/accn/form/filed/tag/unit, verbatim.

    A row whose value cannot coerce to float is surfaced with value None rather than
    dropped — absence must stay visible (Law 7).
    """
    client = client or get_client()
    splits = load_splits(symbol, client)
    rows = (
        client.table("fundamentals_latest")
        .select("*")
        .eq("symbol", symbol)
        .eq("concept", concept)
        .order("period_end")
        .execute()
        .data
        or []
    )
    out: list[dict] = []
    for row in rows:
        try:
            raw = float(row["value"])
        except (KeyError, TypeError, ValueError):
            raw = None
        adjustable = bool(row.get("is_split_adjustable"))
        factor = factor_from_splits(splits, row["filed"]) if adjustable else 1.0
        out.append(
            {
                "symbol": symbol,
                "concept": concept,
                "tag": row.get("tag"),
                "unit": row.get("unit"),
                "period_start": row.get("period_start"),
                "period_end": row.get("period_end"),
                "value": raw * factor if raw is not None else None,
                "value_raw": raw,
                "split_factor": factor,
                "is_split_adjustable": adjustable,
                "accn": row.get("accn"),
                "form": row.get("form"),
                "filed": row.get("filed"),
            }
        )
    return out


def _print_series(symbol: str, concept: str) -> None:
    """The regression probe: the full adjusted series with per-row factor + YoY change."""
    records = read_concept(symbol, concept)
    if not records:
        print(f"[splits] {symbol} {concept}: no rows in fundamentals_latest")
        return
    print(
        f"[splits] {symbol} {concept} — {len(records)} annual rows "
        f"(adjusted = raw × factor; factor from splits with effective_date > filed):"
    )
    print(f"  {'period_end':<12} {'raw value':>16} {'filed':<12} {'x':>4} {'adjusted':>18} {'YoY':>8}")
    prev = None
    for r in records:
        adj = r["value"]
        yoy = ""
        if prev not in (None, 0) and adj is not None:
            yoy = f"{(adj / prev - 1) * 100:+.1f}%"
        print(
            f"  {r['period_end']:<12} {r['value_raw']:>16,.0f} {r['filed']:<12} "
            f"{r['split_factor']:>4.0f} {adj:>18,.0f} {yoy:>8}"
        )
        prev = adj


if __name__ == "__main__":
    import sys

    # The probe header carries '×'/'—'; force UTF-8 stdout so a Windows cp1252 console
    # prints them instead of mojibake (same best-effort reconfigure as journal.checkpoint).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — best-effort; ASCII-only terminals still run fine
        pass
    _print_series("TSLA", "shares_diluted")
