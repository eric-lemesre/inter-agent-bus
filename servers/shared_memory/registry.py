"""Machine registry of the hub — which machines may talk to the bus, with
which certificate, in which role.

Lives in the bus database (one database: owner's decision of 2026-10-17)
next to the bus tables, through the same dialect layer; standard library
only, testable without the network. The hub is the only verifier of
machine certificates, so revocation needs no CRL: a certificate is
accepted when the registry lists its fingerprint for an active machine.

Renewal keeps the previous fingerprint valid until the new certificate
is first seen: a renewal whose answer is lost on the way must not lock
the machine out.
"""
from __future__ import annotations

import re
from contextlib import closing

try:  # installed package (`iab` maps to this directory)
    from . import store
except ImportError:  # bare script usage
    import store

ROLES = ("admin", "member")
KEY_KINDS = ("file", "tpm")
_MACHINE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    name             TEXT PRIMARY KEY,
    role             TEXT NOT NULL,
    key_kind         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    fingerprint      TEXT NOT NULL,
    prev_fingerprint TEXT NOT NULL DEFAULT '',
    serial           TEXT NOT NULL,
    platform         TEXT NOT NULL DEFAULT '',
    enrolled_at      TEXT NOT NULL,
    renewed_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    last_seen        TEXT NOT NULL DEFAULT '',
    revoked_at       TEXT NOT NULL DEFAULT ''
);
"""


def valid_machine_name(name: str) -> bool:
    return bool(_MACHINE_RE.match(name))


def _connect():
    conn = store.connect()
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    return conn


def enroll(
    name: str, role: str, key_kind: str, fingerprint: str, serial: str,
    expires_at: str, platform: str = "",
) -> str:
    """Record a newly paired machine. A revoked name may be paired again
    (a reinstalled machine); an active one may not — revoke it first."""
    name = name.strip().lower()
    if not valid_machine_name(name):
        return f"ERROR: invalid machine name '{name}' — expected [a-z0-9][a-z0-9.-]{{0,62}}."
    if role not in ROLES:
        return f"ERROR: role must be one of {', '.join(ROLES)}."
    if key_kind not in KEY_KINDS:
        return f"ERROR: key kind must be one of {', '.join(KEY_KINDS)}."
    now = store._now()
    with closing(_connect()) as conn, store._write(conn):
        row = conn.execute("SELECT status FROM machines WHERE name=?", (name,)).fetchone()
        if row is not None and row["status"] == "active":
            return f"ERROR: machine '{name}' is already paired — revoke it first."
        conn.execute("DELETE FROM machines WHERE name=?", (name,))
        conn.execute(
            "INSERT INTO machines(name, role, key_kind, status, fingerprint, serial, "
            "platform, enrolled_at, renewed_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, role, key_kind, "active", fingerprint, serial, platform, now, now,
             expires_at),
        )
        store._log_event(conn, "machine", agent=None,
                         detail=f"paired {name} ({role}, key {key_kind})")
    return f"OK: machine '{name}' paired as {role}."


def authenticate(fingerprint: str) -> dict | None:
    """The active machine presenting this certificate, or None. Seeing the
    new certificate of a renewal retires the previous one."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM machines WHERE status='active' AND "
            "(fingerprint=? OR (prev_fingerprint<>'' AND prev_fingerprint=?))",
            (fingerprint, fingerprint),
        ).fetchone()
        if row is None:
            return None
        now = store._now()
        if row["fingerprint"] == fingerprint and row["prev_fingerprint"]:
            conn.execute("UPDATE machines SET prev_fingerprint='', last_seen=? WHERE name=?",
                         (now, row["name"]))
        elif row["last_seen"][:16] != now[:16]:  # at most one write per minute
            conn.execute("UPDATE machines SET last_seen=? WHERE name=?", (now, row["name"]))
        return dict(row)


def renew(name: str, fingerprint: str, serial: str, expires_at: str) -> str:
    with closing(_connect()) as conn, store._write(conn):
        row = conn.execute(
            "SELECT fingerprint FROM machines WHERE name=? AND status='active'", (name,)
        ).fetchone()
        if row is None:
            return f"ERROR: machine '{name}' is not paired."
        conn.execute(
            "UPDATE machines SET prev_fingerprint=?, fingerprint=?, serial=?, "
            "renewed_at=?, expires_at=? WHERE name=?",
            (row["fingerprint"], fingerprint, serial, store._now(), expires_at, name),
        )
        store._log_event(conn, "machine", detail=f"renewed {name} until {expires_at}")
    return f"OK: certificate of '{name}' renewed until {expires_at}."


def revoke(name: str) -> str:
    """Refuse a machine from now on: its certificate (and the previous one)
    stops being accepted at its next request."""
    name = name.strip().lower()
    with closing(_connect()) as conn, store._write(conn):
        row = conn.execute("SELECT status FROM machines WHERE name=?", (name,)).fetchone()
        if row is None:
            return f"ERROR: unknown machine '{name}'."
        if row["status"] == "revoked":
            return f"ERROR: machine '{name}' is already revoked."
        conn.execute(
            "UPDATE machines SET status='revoked', prev_fingerprint='', revoked_at=? "
            "WHERE name=?", (store._now(), name))
        store._log_event(conn, "machine", detail=f"revoked {name}")
    return f"OK: machine '{name}' revoked."


def list_machines() -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT name, role, key_kind, status, platform, enrolled_at, renewed_at, "
            "expires_at, last_seen, revoked_at FROM machines ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def get(name: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM machines WHERE name=?",
                           (name.strip().lower(),)).fetchone()
    return dict(row) if row else None
