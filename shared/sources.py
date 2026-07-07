"""Argus shared — §5 source taxonomy for Source Health (one home, two readers).

The digest's §7 Source-Health verdict (``digest.bundle._aggregate_sources``) and the ``/health``
command (``bot.handlers.handle_health``) both decide which ``fetch_log`` labels count as §5 DATA
feeds. That decision lives here so the two surfaces can never disagree — the drift that let
``telegram_webhook`` (and ``pipeline:*``) redden ``/health`` while the digest already excluded
them. The webhook (``bot.handlers``) imports this; it must never import ``digest`` — topology.
"""

from __future__ import annotations

# Logical fetch_log sources that are NOT §5 data feeds, so they never enter the Source-Health
# DATA verdict (their rows still land in fetch_log — nothing is silenced; forensics intact):
#   • pipeline         — pipeline STEP outcomes (log only on failure, dupe the underlying
#                        source's status; ``pipeline:telegram`` is an outbound push, not an input).
#   • telegram_webhook — the inbound command EAR (Vercel webhook): feeds ZERO data into the
#                        digest and writes fetch_log ONLY on failure, so its last row would
#                        otherwise redden the verdict forever. Including it is a categorization
#                        bug, not a health signal. Webhook liveness is infra (an external prober),
#                        a different question from §5 data health — never folded back in here.
#   • config_read      — in-run reads of the config table (e.g. sleeve_shares inside the Flex
#                        pull; log only on failure). Under the old 'ibkr_flex:config' label the
#                        failure collapsed into the ibkr_flex verdict slot and the section
#                        stores' later successes MASKED it most-recent-wins (PHASE0-TODO #4).
#                        A config read is infra, not a §5 data feed: excluded here on both
#                        surfaces, while the fetch_log row keeps the forensic trail.
#   • analyst, sec_facts — the Phase-5 dossier pipeline's step outcomes and its per-issuer
#                        SEC pulls. They feed the ANALYST module, not the digest — §5's verdict
#                        is about the digest's data feeds — and several labels (analyst:draft)
#                        log only on failure by design (a repaired draft is a normal outcome),
#                        which would otherwise redden /health forever, the exact
#                        telegram_webhook categorization bug. A dossier-run failure surfaces
#                        loud on its own channel: the red analyze.yml run + its Telegram alert.
NON_DATA_SOURCES: frozenset[str] = frozenset(
    {"pipeline", "telegram_webhook", "config_read", "analyst", "sec_facts"}
)


def logical_source(label: str) -> str:
    """Collapse a granular fetch_log label to its logical §5 source (text before ':').

    Fetchers log per-ticker / per-series / per-section labels (``tiingo:TSLA``, ``fred:DFF``,
    ``ibkr_flex:positions``); the §7 health verdict is per logical source (``tiingo``, ``fred``,
    ``ibkr_flex``). A bare label with no ':' is its own source.
    """
    return label.split(":", 1)[0] if ":" in label else label


def is_non_data_source(label: str) -> bool:
    """True if a fetch_log label (raw or granular) belongs to a non-§5-data source.

    Used to EXCLUDE such rows from the Source-Health verdict on both surfaces; matches on the
    logical prefix, so ``pipeline:av_news``, ``pipeline:telegram`` and ``telegram_webhook`` all
    resolve True. It excludes only — surviving rows render exactly as their caller renders them.
    """
    return logical_source(label) in NON_DATA_SOURCES
