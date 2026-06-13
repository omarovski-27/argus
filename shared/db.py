"""Argus shared — Supabase client factory (the spine lives in Postgres; §3)."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from supabase import Client, create_client

# Process-wide singleton (blueprint §3: one spine, read/written from every job).
_client: Client | None = None


def get_client() -> Client:
    """Return a process-wide singleton Supabase client, configured from the env.

    Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY via python-dotenv (a local
    `.env` in dev; injected env vars in GitHub Actions / Vercel — load_dotenv is a
    no-op there and does not override real env vars). The service-role key bypasses
    RLS and is server-side only; it is read from the environment and never hardcoded
    (blueprint §13). The first call constructs the client; every later call in the
    same process returns the same instance.

    Raises:
        RuntimeError: if either required variable is missing — fail loud rather than
            attempt a connection with half a credential.
    """
    global _client
    if _client is None:
        load_dotenv()
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set "
                "(see .env.example). Refusing to create a client without them."
            )
        _client = create_client(url, key)
    return _client
