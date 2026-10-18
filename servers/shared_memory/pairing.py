"""Pairing authority of the hub — turns pairing requests, approvals,
invitations and renewals into certificates and registry rows.

The hub's listeners call this module and only map its answers to HTTP;
the registry (registry.py) holds the state, pki.py the cryptography. Every
answer is a dict (success) or an `ERROR:` string (refusal), like the core.

Policy (owner's decision of 2026-10-17): an admin machine — one that can
approve pairings and revoke machines — must hold a TPM key, attested
(attest.py), never declared. The exception is the machine `iab hub init`
creates on the hub itself: its key sits next to the CA key, which a TPM
would not protect better.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

try:  # installed package (`iab` maps to this directory)
    from . import attest, pki, registry, store
except ImportError:  # bare script usage
    import attest
    import pki
    import registry
    import store

# Key kinds a joining machine may claim. `tpm` holds only with an
# attestation the hub verified; a bare claim proves nothing.
CLAIMABLE_KEY_KINDS = ("file", "tpm")
# A challenge is answered within seconds; unauthenticated, so bounded —
# per source, and overall (the oldest gives way: one source cannot hold
# the table, a legitimate machine retries).
CHALLENGE_TTL = 120
MAX_CHALLENGES = 64
MAX_CHALLENGES_PER_SOURCE = 4


def policy(role: str, key_kind: str) -> str | None:
    if role == "admin" and key_kind != "tpm":
        return ("ERROR: an admin machine must hold an attested TPM key (owner's policy) — "
                "pair it with `iab join --tpm`, or as a member.")
    return None


class Authority:
    """The CA side of the hub: one per hub home, shared by its listeners."""

    def __init__(self, home: Path, config: dict) -> None:
        self.home = Path(home)
        self.config = config
        self.ca_cert = pki.load_cert(self.home / "ca.pem")
        self.ca_pem = (self.home / "ca.pem").read_text(encoding="ascii")
        self._ca_key = pki.load_key(self.home / "ca.key")
        # The manufacturers' CAs shipped with the hub, plus the operator's.
        self.trust = attest.Trust(self.home / "tpm-ca")
        # Challenges live seconds, between two requests of one machine to
        # this process: memory is enough (a restart only makes it retry).
        # id → (deadline, secret digest, key fingerprint, description, source)
        self._challenges: dict[str, tuple[float, bytes, str, str, str]] = {}
        self._challenge_lock = threading.Lock()

    @property
    def bus_url(self) -> str:
        host, port = self.config["names"][0], int(self.config["port"])
        return f"https://{host}" + ("" if port == 443 else f":{port}")

    def _issue(self, public_key, name: str) -> tuple[str, str, str, str]:
        cert = pki.machine_cert(self._ca_key, self.ca_cert, public_key, name)
        return (pki.cert_pem(cert).decode("ascii"), pki.cert_sha256(pki.cert_der(cert)),
                format(cert.serial_number, "x"), pki.not_after(cert).isoformat())

    def _announce(self, message: str) -> None:
        # The owner's notification: a channel entry any session can read.
        store.system_announce("machines", message)

    # -- pairing listener (no client certificate) --------------------------

    def challenge(self, body: dict, source: str) -> dict | str:
        """TPM attestation, step 1 (attest.py): check what the machine says
        about its chip, then seal a secret to its EK for the key of its
        certificate request. The machine answers with its next request."""
        try:
            csr = pki.load_csr(str(body.get("csr", "")).encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            return f"ERROR: unusable certificate request: {exc}."
        checked = attest.check(body.get("tpm"), csr.public_key(), self.trust,
                               registry.trusted_ek)
        if isinstance(checked, str):
            return checked
        secret = secrets.token_bytes(attest.SECRET_BYTES)
        blob, encrypted = attest.make_credential(checked.ek, secret, checked.name)
        challenge_id = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self._challenge_lock:
            self._challenges = {k: v for k, v in self._challenges.items() if v[0] > now}
            if sum(1 for v in self._challenges.values() if v[4] == source) \
                    >= MAX_CHALLENGES_PER_SOURCE:
                return "ERROR: too many TPM attestations in progress from this address."
            if len(self._challenges) >= MAX_CHALLENGES:  # dicts keep insertion order
                del self._challenges[next(iter(self._challenges))]
            self._challenges[challenge_id] = (now + CHALLENGE_TTL,
                                              hashlib.sha256(secret).digest(),
                                              pki.spki_sha256(csr.public_key()),
                                              checked.description, source)
        return {"id": challenge_id, "credential_blob": attest.encode(blob),
                "encrypted_secret": attest.encode(encrypted)}

    def _attested(self, body: dict, csr, key_kind: str) -> str:
        """"" for a file key; for a TPM key, how it was proven, or an ERROR
        line. Each challenge is answered once, for the key it was made for."""
        if key_kind != "tpm":
            return ""
        proof = body.get("attestation")
        if not isinstance(proof, dict):
            return "ERROR: a TPM key must answer the hub's attestation challenge."
        with self._challenge_lock:
            entry = self._challenges.pop(str(proof.get("id", "")), None)
        if entry is None or entry[0] < time.monotonic():
            return "ERROR: TPM attestation unknown or expired — pair again."
        try:
            secret = base64.b64decode(str(proof.get("secret", "")), validate=True)
        except ValueError:
            secret = b""
        if not hmac.compare_digest(hashlib.sha256(secret).digest(), entry[1]):
            return "ERROR: TPM attestation failed: the chip did not return the hub's secret."
        if entry[2] != pki.spki_sha256(csr.public_key()):
            return "ERROR: TPM attestation was made for another key."
        return entry[3]

    def invited(self, body: dict, source: str) -> dict | str:
        """A machine joins with an invitation: paired at once. It proves it
        holds the token by a MAC over its own request (the token never
        travels); the answer proves the CA back the same way."""
        name = str(body.get("name", "")).strip().lower()
        key_kind = str(body.get("key_kind", "file"))
        if key_kind not in CLAIMABLE_KEY_KINDS:
            return f"ERROR: unknown key kind '{key_kind}' — expected file or tpm."
        try:
            csr = pki.load_csr(str(body.get("csr", "")).encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            return f"ERROR: unusable certificate request: {exc}."
        platform = registry.printable(body.get("platform", ""))
        common = {"ca": self.ca_pem, "bus_url": self.bus_url,
                  "hub": self.config["names"][0]}
        token_id = body.get("invitation")
        if not token_id:
            return "ERROR: an invited join needs its invitation id."
        # Before the invitation is spent: a failed proof leaves it usable.
        attestation = self._attested(body, csr, key_kind)
        if attestation.startswith("ERROR"):
            return attestation
        invitation = registry.redeem_invitation(
            str(token_id), name, str(body.get("csr", "")).encode("utf-8"),
            str(body.get("mac", "")), refuse=lambda role: policy(role, key_kind))
        if isinstance(invitation, str):
            return invitation
        cert_pem, fingerprint, serial, expires = self._issue(csr.public_key(), name)
        enrolled = registry.enroll(name, invitation["role"], key_kind, fingerprint, serial,
                                   expires, platform, attestation)
        if enrolled.startswith("ERROR"):
            return enrolled
        self._announce(f"machine '{name}' paired as {invitation['role']} ({key_kind} key) "
                       f"by invitation of {invitation['created_by']} (from {source})")
        return {**common, "status": "approved", "name": name, "certificate": cert_pem,
                "proof": pki.invitation_proof(invitation["key"], self.ca_cert, self.bus_url)}

    def commit(self, body: dict, source: str) -> dict | str:
        """Pairing by code, step 1: the machine commits to its request."""
        name = str(body.get("name", "")).strip().lower()
        key_kind = str(body.get("key_kind", "file"))
        if key_kind not in CLAIMABLE_KEY_KINDS:
            return f"ERROR: unknown key kind '{key_kind}' — expected file or tpm."
        created = registry.create_request(name, str(body.get("platform", "")), key_kind,
                                          source, str(body.get("commitment", "")))
        if isinstance(created, str):
            return created
        return {"ca": self.ca_pem, "bus_url": self.bus_url, "hub": self.config["names"][0],
                **created}

    def reveal(self, body: dict) -> dict | str:
        """Pairing by code, step 2: the request is revealed and must match
        the commitment; the check words are computed from both sides'
        contributions and shown to the approver later."""
        device_code = str(body.get("device_code", ""))
        waiting = registry.committed_request(device_code)
        if waiting is None:
            return "ERROR: no pairing request waiting for this reveal (expired?)."
        csr_text = str(body.get("csr", ""))
        nonce_joiner = str(body.get("nonce", ""))
        if not nonce_joiner.isascii() or len(nonce_joiner) < 32:
            return "ERROR: the reveal needs the machine's nonce."
        if pki.commitment(csr_text.encode("utf-8"), nonce_joiner) != waiting["commitment"]:
            return "ERROR: the revealed request does not match its commitment."
        try:
            csr = pki.load_csr(csr_text.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            return f"ERROR: unusable certificate request: {exc}."
        attestation = self._attested(body, csr, waiting["key_kind"])
        if attestation.startswith("ERROR"):
            return attestation
        words = pki.check_words(self.ca_cert, csr.public_key(), nonce_joiner,
                                waiting["nonce_hub"], self.bus_url)
        return registry.reveal_request(device_code, csr_text, words, attestation)

    def poll(self, body: dict) -> dict:
        return registry.poll_request(str(body.get("device_code", "")))

    # -- admin actions (bus listener, admin machines only) ------------------

    def lookup(self, user_code: str) -> dict | str:
        found = registry.lookup_request(user_code)
        if found is None:
            return (f"ERROR: no pending pairing request with code "
                    f"{registry.normalize_code(user_code)} (mistyped, expired or decided).")
        # A name paired before (then revoked) still owns its instances: the
        # approver is shown what the new machine would inherit.
        inherits = sorted(a for a, m in registry.bindings().items() if m == found["name"])
        return {**found, "inherits": inherits}

    def approve(self, user_code: str, name: str | None, role: str, approver: str) -> dict | str:
        found = registry.lookup_request(user_code)
        if found is None:
            return self.lookup(user_code)
        refused = policy(role, found["key_kind"])
        if refused:
            return refused
        csr_pem = registry.request_csr(user_code)
        if csr_pem is None:
            return self.lookup(user_code)
        final = (name or found["name"]).strip().lower()
        csr = pki.load_csr(csr_pem.encode("ascii"))
        cert_pem, fingerprint, serial, expires = self._issue(csr.public_key(), final)
        done = registry.approve_request(user_code, final, role, found["key_kind"], cert_pem,
                                        fingerprint, serial, expires, approver)
        if done.startswith("ERROR"):
            return done
        self._announce(f"machine '{final}' paired as {role} ({found['key_kind']} key), "
                       f"approved by {approver} (request from {found['source']})")
        return {"result": done, "name": final, "expires_at": expires}

    def deny(self, user_code: str, approver: str) -> str:
        return registry.deny_request(user_code, approver)

    def invite(self, name: str, role: str, ttl: int, admin: str) -> dict | str:
        # An admin invitation is redeemed only with an attested TPM key
        # (policy, checked at redemption): the token alone grants nothing more.
        token = registry.create_invitation(name, role, admin, ttl)
        if token.startswith("ERROR"):
            return token
        return {"token": token, "name": name.strip().lower(), "expires_in": ttl}

    def revoke(self, name: str, admin: str) -> str:
        name = name.strip().lower()
        target = registry.get(name)
        if target and target["role"] == "admin" and target["status"] == "active":
            admins = [m for m in registry.list_machines()
                      if m["role"] == "admin" and m["status"] == "active"]
            if len(admins) == 1:
                return ("ERROR: refusing to revoke the last admin machine — nobody could "
                        "approve a pairing any more.")
        done = registry.revoke(name)
        if done.startswith("OK"):
            self._announce(f"machine '{name}' revoked by {admin}")
        return done

    def trust_ek(self, fingerprint: str, note: str, admin: str) -> str:
        value = attest.normalize_fingerprint(fingerprint)
        if value is None:
            return "ERROR: an EK fingerprint is 64 hexadecimal characters (SHA-256)."
        done = registry.trust_ek(value, note, admin)
        if done.startswith("OK"):
            shown = registry.printable(note, 120)
            self._announce(f"TPM endorsement key {value} trusted by {admin}"
                           + (f" ({shown})" if shown else ""))
        return done

    def untrust_ek(self, fingerprint: str, admin: str) -> str:
        value = attest.normalize_fingerprint(fingerprint)
        if value is None:
            return "ERROR: an EK fingerprint is 64 hexadecimal characters (SHA-256)."
        done = registry.untrust_ek(value, admin)
        if done.startswith("OK"):
            self._announce(f"TPM endorsement key {value} withdrawn by {admin}")
        return done

    def machines(self) -> list[dict]:
        """Paired machines, each with the agent instances bound to it."""
        bound: dict[str, list[str]] = {}
        for agent, machine in sorted(registry.bindings().items()):
            bound.setdefault(machine, []).append(agent)
        return [{**m, "agents": bound.get(m["name"], [])} for m in registry.list_machines()]

    def renew(self, machine: dict, peer_der: bytes) -> dict | str:
        """A fresh certificate for the key the machine just proved it holds
        (mutual TLS): no request to sign, the key never changes — which is
        also what a TPM-held key needs."""
        current = pki.load_cert_der(peer_der)
        cert_pem, fingerprint, serial, expires = self._issue(current.public_key(),
                                                              machine["name"])
        done = registry.renew(machine["name"], fingerprint, serial, expires,
                              presented=pki.cert_sha256(peer_der))
        if done.startswith("ERROR"):
            return done
        return {"certificate": cert_pem, "expires_at": expires}


def load(home: Path) -> Authority:
    config = json.loads((Path(home) / "hub.json").read_text(encoding="utf-8"))
    return Authority(home, config)
