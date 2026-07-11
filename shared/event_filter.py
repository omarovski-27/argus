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

from datetime import date

# Calendar event types that arm the 24h no-round-trip filter (blueprint §8 / §202). The
# single definition of WHICH TYPES can arm; the arm DECISION itself is triggers_event_filter.
# Used as the morning push's coarse fetch filter and inside the predicate below — one tuple,
# so the type set cannot drift between the two surfaces.
FILTERED_EVENT_TYPES: tuple[str, ...] = ("fomc", "cpi", "nfp", "earnings", "lockup", "index")

# Plain-fact statements of the rule, rendered VERBATIM in the digest's serialized block so
# the model states them as-is rather than paraphrasing into a nudge (Law 1: describes the
# filter's action, never directs Omar). TWO tenses, chosen by proximity to the digest date
# (§7 wording): only an event inside the 24h window is a rule IN EFFECT — voicing a
# next-week event as "filter active" would state a future condition as a present fact (L2).
EVENT_FILTER_RULE_ACTIVE = "event filter IN EFFECT — sleeve round trips blocked within 24h (§8)"
EVENT_FILTER_RULE_FORWARD = "will trigger the §8 event filter (blocks sleeve round trips within 24h)"
# The morning-warning push (bot.event_filter_check) fires the afternoon before an arming
# event — always inside the 24h window — so it states the rule in the active tense. Kept
# HERE so all §8 rule wording has one home (the §7 single-source goal); the push imports it.
EVENT_FILTER_WARNING = "event filter active — no sleeve round trips within 24h (§8)"


def event_filter_phrase(event_date: str | None, reference_date: str | None) -> str:
    """The §7 calendar tag for an ARMING event: present tense only inside the 24h window.

    Date-granularity approximation of §8's 24h window: the rule is "in effect" on the
    reference (digest) date iff the event falls on that date or the next day. Anything
    later — or any unparseable/missing date — gets the forward phrasing: when proximity
    cannot be established, the WEAKER claim is the honest one (Law 2). The calendar is
    forward-only (next 14 days), so past-dated events do not reach this function.
    """
    try:
        delta = (date.fromisoformat(str(event_date)) - date.fromisoformat(str(reference_date))).days
    except (TypeError, ValueError):
        return EVENT_FILTER_RULE_FORWARD
    return EVENT_FILTER_RULE_ACTIVE if 0 <= delta <= 1 else EVENT_FILTER_RULE_FORWARD


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
