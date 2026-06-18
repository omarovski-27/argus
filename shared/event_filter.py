"""Argus — the §8 event-filter rule, decided in ONE place (single source of truth).

The event filter suppresses sleeve round trips within 24h of certain forward-calendar
events (FOMC / CPI / NFP / earnings / lockup / index — blueprint §8 / §202). TWO surfaces
act on that rule and must agree exactly:

  * the daily morning-warning job (``bot.event_filter_check``), which pushes a heads-up
    the afternoon before such an event; and
  * the weekly digest's Forward Calendar (``digest.serialize``), which tags the same
    events so the synthesis states the rule as a grounded fact.

Both call ``triggers_event_filter`` per calendar row, so the ARM DECISION lives in exactly
one function — not as a predicate in one place and a query filter in another. That is the
drift-safety this module exists for: when §8 arming grows beyond bare type membership (e.g.
"earnings of a *traded* ticker" — arm only when the row's symbol is on the book), the new
condition is added HERE once and both surfaces inherit it. The morning push fetches the
day's candidate rows (coarsely, by the shared type set) and filters them through this
predicate; the digest tags rows through this predicate. Neither re-encodes the decision.

Information, never instruction (Law 1): everything here states what the filter DOES; none
of it tells Omar to trade or not. The digest renders the rule beside an arming event; the
morning job warns the day before. Omar decides.
"""

from __future__ import annotations

# Calendar event types that arm the 24h no-round-trip filter (blueprint §8 / §202). The
# single definition of WHICH TYPES can arm; the arm DECISION itself is triggers_event_filter.
# Used as the morning push's coarse fetch filter and inside the predicate below — one tuple,
# so the type set cannot drift between the two surfaces.
FILTERED_EVENT_TYPES: tuple[str, ...] = ("fomc", "cpi", "nfp", "earnings", "lockup", "index")

# Plain-fact statement of the rule, rendered VERBATIM in the digest's serialized block so
# the model states it as-is rather than paraphrasing it into a nudge (Law 1: describes the
# filter's action, never directs Omar). Mirrors the morning warning's phrasing.
EVENT_FILTER_RULE_TEXT = "event filter — blocks sleeve round trips within 24h (§8)"


def triggers_event_filter(event: dict) -> bool:
    """True if a ``calendar_events`` row arms the §8 24h filter — the one arm decision.

    Both the morning-warning job and the digest's Forward Calendar call THIS per row, so the
    rule has a single home and cannot drift between the two surfaces. Today arming is pure
    type membership (``type in FILTERED_EVENT_TYPES``); materiality no longer gates it (it
    stays on the row for ordering/display only). When §8 needs a richer condition — e.g.
    "earnings of a traded ticker" keys on ``event['symbol']`` against the book — add it here
    and both surfaces inherit it unchanged.
    """
    return ((event or {}).get("type") or "") in FILTERED_EVENT_TYPES
