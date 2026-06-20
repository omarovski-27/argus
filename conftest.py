"""Pytest root conftest.

Its presence makes the repo root the rootdir, so pytest prepends it to sys.path and
the package imports (``journal.pairing``, ``shared.*``) resolve in tests without any
per-file path hacks. Intentionally empty otherwise.
"""
