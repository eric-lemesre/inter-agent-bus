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

import hashlib
import hmac
import re
import secrets
from contextlib import closing
from datetime import datetime, timedelta, timezone

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
CREATE TABLE IF NOT EXISTS pairing_requests (
    device_hash TEXT PRIMARY KEY,
    user_code   TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    platform    TEXT NOT NULL DEFAULT '',
    key_kind    TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    commitment  TEXT NOT NULL,
    nonce_hub   TEXT NOT NULL,
    csr         TEXT NOT NULL DEFAULT '',
    words       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'committed',
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    last_poll   TEXT NOT NULL DEFAULT '',
    cert        TEXT NOT NULL DEFAULT '',
    decided_by  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agent_machines (
    agent    TEXT PRIMARY KEY,
    machine  TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    pinned   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invitations (
    token_hash TEXT PRIMARY KEY,
    mac_key    TEXT NOT NULL,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT NOT NULL DEFAULT ''
);
"""

# RFC 8628 § 6.1: consonants only (no vowel, so no word can form), no
# look-alike letter — 20^8 ≈ 2.6e10 codes, typed in two groups of four.
CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
PAIRING_TTL = 600
POLL_INTERVAL = 5
MAX_PENDING = 20
MAX_REQUESTS_PER_SOURCE = 5  # per PAIRING_TTL window
INVITATION_TTL = 900


def valid_machine_name(name: str) -> bool:
    return bool(_MACHINE_RE.match(name))


def _connect():
    conn = store.connect()
    conn.dialect.prepare(conn, _create)
    return conn


def _create(conn) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(conn.dialect.ddl(statement))


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
    platform = printable(platform)
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


# -- pairing (RFC 8628 device authorization, adapted) -----------------------


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# An invitation token never travels: the joining machine sends an id and
# a MAC derived from it, so a party in the middle of an unverified first
# contact learns nothing it could redeem for its own key.
def invitation_id(token: str) -> str:
    return hashlib.sha256(b"iab-invite-id\0" + token.encode("utf-8")).hexdigest()


def invitation_key(token: str) -> bytes:
    return hashlib.sha256(b"iab-invite-key\0" + token.encode("utf-8")).digest()


def invitation_mac(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def _in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def normalize_code(text: str) -> str:
    """Accept a code typed with or without its dash, any case, spaces."""
    raw = re.sub(r"[^A-Za-z]", "", text).upper()
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else raw


def _new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _expire(conn) -> None:
    conn.execute("UPDATE pairing_requests SET status='expired', csr='', cert='' "
                 "WHERE status IN ('committed', 'pending', 'approved') AND expires_at < ?",
                 (store._now(),))


def printable(text: str, limit: int = 80) -> str:
    """Strip what a terminal would interpret (control, format and other
    non-printable characters): a displayed field must not be able to
    redraw the approval dialogue."""
    return "".join(c for c in str(text) if c.isprintable())[:limit]


def create_request(name: str, platform: str, key_kind: str, source: str,
                   commitment: str) -> dict | str:
    """Open a pairing request from a COMMITMENT to the certificate request
    (step 1 of 2, see pairing.py). Returns {device_code, nonce_hub,
    expires_in}: the device code is a secret known only to the joining
    machine; the hub's nonce is drawn only now, after the commitment."""
    name = name.strip().lower()
    if not valid_machine_name(name):
        return f"ERROR: invalid machine name '{name}' — expected [a-z0-9][a-z0-9.-]{{0,62}}."
    if key_kind not in KEY_KINDS:
        return f"ERROR: key kind must be one of {', '.join(KEY_KINDS)}."
    if not re.fullmatch(r"[0-9a-f]{64}", commitment or ""):
        return "ERROR: the pairing commitment must be a SHA-256 hex digest."
    platform = printable(platform)
    device_code = secrets.token_urlsafe(32)
    nonce_hub = secrets.token_hex(32)
    since = (datetime.now(timezone.utc) - timedelta(seconds=PAIRING_TTL)).isoformat()
    with closing(_connect()) as conn, store._write(conn):
        _expire(conn)
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM pairing_requests "
            "WHERE status IN ('committed', 'pending')").fetchone()["n"]
        if pending >= MAX_PENDING:
            return "ERROR: too many pairing requests waiting — try again in a few minutes."
        recent = conn.execute(
            "SELECT COUNT(*) AS n FROM pairing_requests WHERE source=? AND created_at >= ?",
            (source, since)).fetchone()["n"]
        if recent >= MAX_REQUESTS_PER_SOURCE:
            return "ERROR: too many pairing requests from this address — slow down."
        for _ in range(10):
            code = _new_code()
            if conn.execute("SELECT 1 FROM pairing_requests WHERE user_code=?",
                            (code,)).fetchone() is None:
                break
        else:  # pragma: no cover — 2.6e10 codes, at most MAX_PENDING alive
            return "ERROR: could not allocate a pairing code."
        conn.execute(
            "INSERT INTO pairing_requests(device_hash, user_code, name, platform, key_kind, "
            "source, commitment, nonce_hub, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (_hash(device_code), code, name, platform, key_kind, source, commitment,
             nonce_hub, store._now(), _in(PAIRING_TTL)),
        )
        store._log_event(conn, "machine", detail=f"pairing requested for {name} from {source}")
    return {"device_code": device_code, "nonce_hub": nonce_hub, "expires_in": PAIRING_TTL}


def committed_request(device_code: str) -> dict | None:
    """A request waiting for its reveal (step 2): commitment and nonce."""
    with closing(_connect()) as conn:
        _expire(conn)
        row = conn.execute(
            "SELECT commitment, nonce_hub, user_code FROM pairing_requests "
            "WHERE device_hash=? AND status='committed'", (_hash(device_code),)).fetchone()
    return dict(row) if row else None


def reveal_request(device_code: str, csr_pem: str, words: str) -> dict | str:
    """Step 2: the revealed request matched its commitment (checked by the
    caller); the request now waits for a person. Returns {user_code,
    expires_in, interval}."""
    with closing(_connect()) as conn, store._write(conn):
        _expire(conn)
        row = conn.execute(
            "SELECT user_code, expires_at FROM pairing_requests "
            "WHERE device_hash=? AND status='committed'", (_hash(device_code),)).fetchone()
        if row is None:
            return "ERROR: no pairing request waiting for this reveal (expired?)."
        conn.execute("UPDATE pairing_requests SET status='pending', csr=?, words=? "
                     "WHERE device_hash=?", (csr_pem, words, _hash(device_code)))
    left = (datetime.fromisoformat(row["expires_at"]) - datetime.now(timezone.utc))
    return {"user_code": row["user_code"], "expires_in": max(0, int(left.total_seconds())),
            "interval": POLL_INTERVAL}


def poll_request(device_code: str) -> dict:
    """What the joining machine learns when it polls: `pending`,
    `slow_down` (polled faster than the interval), `approved` with its
    certificate (handed out once), `denied` or `expired`."""
    with closing(_connect()) as conn, store._write(conn):
        _expire(conn)
        row = conn.execute("SELECT * FROM pairing_requests WHERE device_hash=?",
                           (_hash(device_code),)).fetchone()
        if row is None:
            return {"status": "expired"}
        now = datetime.now(timezone.utc)
        if row["last_poll"] and row["status"] == "pending":
            last = datetime.fromisoformat(row["last_poll"])
            if (now - last).total_seconds() < POLL_INTERVAL - 1:
                return {"status": "slow_down", "interval": POLL_INTERVAL}
        conn.execute("UPDATE pairing_requests SET last_poll=? WHERE device_hash=?",
                     (now.isoformat(), row["device_hash"]))
        if row["status"] == "approved":
            conn.execute("UPDATE pairing_requests SET status='delivered', cert='', csr='' "
                         "WHERE device_hash=?", (row["device_hash"],))
            return {"status": "approved", "name": row["name"], "certificate": row["cert"]}
        if row["status"] in ("delivered", "committed"):
            return {"status": "expired"}  # a committed request is never polled
        return {"status": row["status"]}


def lookup_request(user_code: str) -> dict | None:
    """A pending request as an approver sees it (no secret, no CSR)."""
    with closing(_connect()) as conn:
        _expire(conn)
        row = conn.execute(
            "SELECT user_code, name, platform, key_kind, source, words, created_at, "
            "expires_at FROM pairing_requests WHERE user_code=? AND status='pending'",
            (normalize_code(user_code),)).fetchone()
    return dict(row) if row else None


def request_csr(user_code: str) -> str | None:
    with closing(_connect()) as conn:
        _expire(conn)
        row = conn.execute(
            "SELECT csr FROM pairing_requests WHERE user_code=? AND status='pending'",
            (normalize_code(user_code),)).fetchone()
    return row["csr"] if row else None


def approve_request(user_code: str, name: str, role: str, key_kind: str, cert_pem: str,
                    fingerprint: str, serial: str, expires_at: str, approver: str) -> str:
    """Approve a pending request and pair the machine — one transaction:
    the certificate is never handed out without the machine being listed,
    nor listed without its request being settled."""
    code = normalize_code(user_code)
    name = name.strip().lower()
    if role not in ROLES:
        return f"ERROR: role must be one of {', '.join(ROLES)}."
    if not valid_machine_name(name):
        return f"ERROR: invalid machine name '{name}'."
    now = store._now()
    with closing(_connect()) as conn, store._write(conn):
        _expire(conn)
        row = conn.execute("SELECT status FROM pairing_requests WHERE user_code=?",
                           (code,)).fetchone()
        if row is None or row["status"] != "pending":
            return f"ERROR: no pending pairing request with code {code} (expired or decided)."
        machine = conn.execute("SELECT status FROM machines WHERE name=?", (name,)).fetchone()
        if machine is not None and machine["status"] == "active":
            return f"ERROR: machine '{name}' is already paired — revoke it first."
        conn.execute("DELETE FROM machines WHERE name=?", (name,))
        conn.execute(
            "INSERT INTO machines(name, role, key_kind, status, fingerprint, serial, "
            "platform, enrolled_at, renewed_at, expires_at) "
            "SELECT ?, ?, ?, 'active', ?, ?, platform, ?, ?, ? FROM pairing_requests "
            "WHERE user_code=?",
            (name, role, key_kind, fingerprint, serial, now, now, expires_at, code),
        )
        conn.execute("UPDATE pairing_requests SET status='approved', cert=?, name=?, "
                     "decided_by=? WHERE user_code=?", (cert_pem, name, approver, code))
        store._log_event(conn, "machine",
                         detail=f"paired {name} ({role}, key {key_kind}) approved by {approver}")
    return f"OK: machine '{name}' paired as {role}."


def deny_request(user_code: str, approver: str) -> str:
    code = normalize_code(user_code)
    with closing(_connect()) as conn, store._write(conn):
        _expire(conn)
        cur = conn.execute(
            "UPDATE pairing_requests SET status='denied', csr='', decided_by=? "
            "WHERE user_code=? AND status='pending'", (approver, code))
        if not cur.rowcount:
            return f"ERROR: no pending pairing request with code {code}."
        store._log_event(conn, "machine", detail=f"pairing {code} denied by {approver}")
    return f"OK: pairing request {code} denied."


def create_invitation(name: str, role: str, created_by: str,
                      ttl: int = INVITATION_TTL) -> str:
    """A one-time token that pairs `name` without a human comparison — the
    path of machines deployed by Ansible. Only its hash is stored."""
    name = name.strip().lower()
    if not valid_machine_name(name):
        return f"ERROR: invalid machine name '{name}'."
    if role not in ROLES:
        return f"ERROR: role must be one of {', '.join(ROLES)}."
    token = secrets.token_urlsafe(32)
    with closing(_connect()) as conn, store._write(conn):
        conn.execute(
            "INSERT INTO invitations(token_hash, mac_key, name, role, created_by, "
            "created_at, expires_at) VALUES(?,?,?,?,?,?,?)",
            (invitation_id(token), invitation_key(token).hex(), name, role, created_by,
             store._now(), _in(ttl)))
        store._log_event(conn, "machine", detail=f"invitation for {name} by {created_by}")
    return token


def redeem_invitation(token_id: str, name: str, signed: bytes, mac: str) -> dict | str:
    """Consume an invitation for `name`: refused when unknown, used,
    expired, issued for another name, or when `mac` is not the MAC of
    `signed` (the certificate request) under the invitation's key — proof
    that the requester holds the token, bound to ITS key. Returns the
    invitation with its key (to prove the CA back)."""
    name = name.strip().lower()
    with closing(_connect()) as conn, store._write(conn):
        row = conn.execute("SELECT * FROM invitations WHERE token_hash=?",
                           (token_id,)).fetchone()
        if row is None or row["used_at"] or row["expires_at"] < store._now():
            return "ERROR: invitation unknown, already used or expired."
        key = bytes.fromhex(row["mac_key"])
        if not hmac.compare_digest(invitation_mac(key, signed), mac):
            return "ERROR: invitation unknown, already used or expired."
        if row["name"] != name:
            return f"ERROR: this invitation is for '{row['name']}', not '{name}'."
        conn.execute("UPDATE invitations SET used_at=? WHERE token_hash=?",
                     (store._now(), row["token_hash"]))
    return {"name": row["name"], "role": row["role"], "created_by": row["created_by"],
            "key": key}


# -- agent instances bound to machines (authorization, phase 9d) -----------


def reserve(agent: str, machine: str) -> tuple[str | None, bool]:
    """May `machine` act as `agent`? Returns (None, reserved) when it may —
    `reserved` is True when this call just bound a NEW name to it (undo it
    with release() if the operation then fails) — or (ERROR line, False).

    Binding on first use applies to names nobody registered yet: an
    existing instance that no machine owns (imported, or registered
    before the hub enforced bindings) is refused until an admin binds it
    (`iab bind`) — a paired machine must not be able to squat it."""
    agent = agent.strip().lower()
    with closing(_connect()) as conn, store._write(conn):
        row = conn.execute("SELECT machine FROM agent_machines WHERE agent=?",
                           (agent,)).fetchone()
        if row is not None:
            if row["machine"] == machine:
                return None, False
            return (f"ERROR: agent '{agent}' belongs to machine '{row['machine']}' — "
                    f"refused from '{machine}' (an admin can move it: "
                    f"`iab bind {agent} {machine}`)."), False
        if conn.execute("SELECT 1 FROM agents WHERE name=?", (agent,)).fetchone():
            return (f"ERROR: agent '{agent}' exists but no machine owns it — an admin must "
                    f"bind it first: `iab bind {agent} {machine}`."), False
        conn.execute("INSERT INTO agent_machines(agent, machine, bound_at) VALUES(?,?,?)",
                     (agent, machine, store._now()))
        store._log_event(conn, "bind", agent=agent, detail=f"bound to machine {machine}")
    return None, True


def release(agent: str, machine: str) -> None:
    """Undo a reservation whose operation failed (never a pinned binding)."""
    with closing(_connect()) as conn, store._write(conn):
        conn.execute("DELETE FROM agent_machines WHERE agent=? AND machine=? AND pinned=0",
                     (agent.strip().lower(), machine))


def bound_machine(agent: str) -> str | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT machine FROM agent_machines WHERE agent=?",
                           (agent.strip().lower(),)).fetchone()
    return row["machine"] if row else None


def pinned_machine(agent: str) -> str | None:
    """The machine an ADMIN bound this instance to — the only bindings that
    count as principals in the hub policy."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT machine FROM agent_machines WHERE agent=? AND pinned=1",
                           (agent.strip().lower(),)).fetchone()
    return row["machine"] if row else None


def pin(agent: str, machine: str, admin: str) -> str:
    """An admin binds an instance to a machine (creates or moves the
    binding, marked pinned)."""
    agent = agent.strip().lower()
    machine = machine.strip().lower()
    if not store._NAME_RE.match(agent):
        return f"ERROR: invalid agent name '{agent}'."
    with closing(_connect()) as conn, store._write(conn):
        row = conn.execute("SELECT status FROM machines WHERE name=?", (machine,)).fetchone()
        if row is None or row["status"] != "active":
            return f"ERROR: no active machine '{machine}'."
        conn.execute("DELETE FROM agent_machines WHERE agent=?", (agent,))
        conn.execute("INSERT INTO agent_machines(agent, machine, bound_at, pinned) "
                     "VALUES(?,?,?,1)", (agent, machine, store._now()))
        store._log_event(conn, "bind", agent=agent,
                         detail=f"bound to machine {machine} by {admin}")
    return f"OK: agent '{agent}' bound to machine '{machine}'."


def rename_binding(old: str, new: str) -> None:
    with closing(_connect()) as conn, store._write(conn):
        conn.execute("UPDATE agent_machines SET agent=? WHERE agent=?",
                     (new.strip().lower(), old.strip().lower()))


def bindings() -> dict[str, str]:
    with closing(_connect()) as conn:
        return {r["agent"]: r["machine"]
                for r in conn.execute("SELECT agent, machine FROM agent_machines")}
