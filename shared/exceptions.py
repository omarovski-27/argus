"""Argus shared — typed exceptions for the ingestion / reliability layer."""


class FetchError(Exception):
    """Raised when a wrapped fetch exhausts all retries without a usable response.

    Law 7 (silent failure is misinformation): a fetch that cannot succeed must fail
    loudly so the caller can surface it (Source Health line, staleness flags, critical
    alerts) — it is never swallowed or papered over with stale or empty data.

    Attributes:
        source:  the data source that failed (e.g. 'tiingo', 'fred', 'ibkr_flex').
        message: human-readable detail about the failure.
    """

    def __init__(self, source: str, message: str) -> None:
        """Build a FetchError carrying the failing `source` and a human `message`."""
        self.source = source
        self.message = message
        super().__init__(f"[{source}] {message}")
