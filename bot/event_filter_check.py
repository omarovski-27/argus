"""Argus bot — daily event-filter morning warning (blueprint §8 / §2 item 10).

Run by GitHub Actions at 12:30 UTC on weekdays (= 15:30 Amman). The morning before,
it surfaces two trade-suppressing conditions so Omar sees them before the open:

  • a calendar event TOMORROW that arms the §8 filter (FOMC / CPI / NFP / earnings /
    lockup / index — decided by shared.event_filter.triggers_event_filter)
    → the event filter is active: no round trips within 24h (§8);
  • the weekly round-trip cap already reached (§8: max 2 / calendar week).

Information, never instruction (Law 1): it states the fact (event / cap); it never
issues 'do not trade' as an order — Omar decides. Law 7: any failure is logged to
``fetch_log`` and re-raised so the scheduled job fails loud — a silent miss on an event
day is exactly the misinformation this guard exists to prevent.

Run:  python -m bot.event_filter_check
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import time
import uuid
from datetime import date, datetime, timedelta, timezone

from bot.telegram import send_message
from shared.db import get_client
from shared.event_filter import (
    EVENT_FILTER_WARNING,
    FILTERED_EVENT_TYPES,
    triggers_event_filter,
)
from shared.fetch_logger import write_fetch_log

# Default weekly cap (§8); overridable via config.weekly_trade_cap. (§8 arming itself is
# decided by shared.event_filter.triggers_event_filter — the same predicate the digest uses,
# called per row in check_and_warn below; this module never re-encodes the arm rule.)
_DEFAULT_WEEKLY_CAP = 2


def _event_label(row: dict) -> str:
    """Render an event as 'SPCX lockup' (ticker-specific) or 'FOMC' (macro)."""
    symbol = row.get("symbol")
    return f"{symbol} {row['type']}" if symbol else (row.get("type") or "").upper()


def _week_bounds_utc(today: date) -> tuple[str, str]:
    """Return (Monday, Sunday) ISO dates of the calendar week containing ``today`` (UTC)."""
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for fetch_log)."""
    return int((time.monotonic() - start) * 1000)


def check_and_warn() -> None:
    """Push the morning event-filter / weekly-cap warning when warranted (§8).

    Sends nothing when neither condition holds (Law 8: quiet unless there's a fact to
    report). When both hold, they are combined into a single message. Law 7: on any
    failure, log to ``fetch_log`` and re-raise so the GitHub Actions run fails visibly.
    """
    run_id = f"event-filter-{uuid.uuid4().hex[:12]}"
    start = time.monotonic()
    try:
        client = get_client()
        today = datetime.now(timezone.utc).date()
        tomorrow = (today + timedelta(days=1)).isoformat()

        # §8 arming is decided in ONE place: shared.event_filter.triggers_event_filter, the
        # same predicate the digest's Forward Calendar uses. Fetch tomorrow's candidate rows
        # COARSELY by type (the shared FILTERED_EVENT_TYPES set — can't drift), then let the
        # predicate make the arm call per row. When arming grows past bare type (e.g.
        # "earnings of a traded ticker"), only the predicate changes and the push inherits it.
        rows = (
            client.table("calendar_events")
            .select("type,symbol,materiality")
            .eq("date", tomorrow)
            .in_("type", list(FILTERED_EVENT_TYPES))
            .execute()
            .data
        ) or []
        events = [row for row in rows if triggers_event_filter(row)]

        cap_resp = (
            client.table("config")
            .select("value")
            .eq("key", "weekly_trade_cap")
            .limit(1)
            .execute()
        )
        weekly_cap = int(cap_resp.data[0]["value"]) if cap_resp.data else _DEFAULT_WEEKLY_CAP

        monday, sunday = _week_bounds_utc(today)
        trips_resp = (
            client.table("round_trips")
            .select("id", count="exact")
            .gte("date", monday)
            .lte("date", sunday)
            .execute()
        )
        trips_this_week = (
            trips_resp.count if trips_resp.count is not None else len(trips_resp.data)
        )

        messages: list[str] = []
        if events:
            labels = ", ".join(sorted({_event_label(event) for event in events}))
            messages.append(f"⚠️ {labels} tomorrow — {EVENT_FILTER_WARNING}.")
        if trips_this_week >= weekly_cap:
            messages.append(
                f"⛔ {trips_this_week}/{weekly_cap} trades this week — weekly cap reached."
            )

        if messages:
            send_message("\n".join(messages))
            print(f"[event_filter] sent {len(messages)} warning(s).")
        else:
            print("[event_filter] no event tomorrow, cap not reached — no warning sent.")
    except Exception as exc:  # noqa: BLE001 — surface, never swallow (Law 7)
        write_fetch_log("event_filter", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[event_filter] FAILED — {exc}")
        raise


if __name__ == "__main__":
    check_and_warn()
