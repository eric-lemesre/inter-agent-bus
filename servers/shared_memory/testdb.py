"""Test helper: fresh bus databases on the engine under test.

By default a SQLite file; with IAB_TEST_POSTGRES=<URL of an EMPTY test
database> a new PostgreSQL schema per call (selected by search_path in
the URL), so the same checks run on both engines.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

PG = os.environ.get("IAB_TEST_POSTGRES", "")


def fresh_db(tmp, label: str) -> str:
    """A new, empty bus database: a file, or a new PostgreSQL schema."""
    if not PG:
        return str(Path(tmp) / f"{label}.db")
    import psycopg
    schema = f"t_{label}_{os.getpid()}_{time.time_ns() % 10**9}"
    with psycopg.connect(PG, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    joiner = "&" if "?" in PG else "?"
    return f"{PG}{joiner}options=-csearch_path%3D{schema}"
