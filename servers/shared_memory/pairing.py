"""Pairing authority of the hub — turns pairing requests, approvals,
invitations and renewals into certificates and registry rows.

The hub's listeners call this module and only map its answers to HTTP;
the registry (registry.py) holds the state, pki.py the cryptography. Every
answer is a dict (success) or an `ERROR:` string (refusal), like the core.

Policy (owner's decision of 2026-10-17): an admin machine — one that can
approve pairings and revoke machines — must hold a TPM key. Until a
machine can prove it (phase 9e), the only admin is the one `iab hub init`
creates locally.
"""
from __future__ import annotations

import json
from pathlib import Path

try:  # installed package (`iab` maps to this directory)
    from . import pki, registry, store
except ImportError:  # bare script usage
    import pki
    import registry
    import store

# Key kinds a joining machine may claim today. `tpm` needs an attestation
# the hub can verify (phase 9e); a bare claim proves nothing.
CLAIMABLE_KEY_KINDS = ("file",)


def policy(role: str, key_kind: str) -> str | None:
    if role == "admin" and key_kind != "tpm":
        return ("ERROR: an admin machine must hold a TPM key (owner's policy) — pair it "
                "as a member.")
    return None


class Authority:
    """The CA side of the hub: one per hub home, shared by its listeners."""

    def __init__(self, home: Path, config: dict) -> None:
        self.home = Path(home)
        self.config = config
        self.ca_cert = pki.load_cert(self.home / "ca.pem")
        self.ca_pem = (self.home / "ca.pem").read_text(encoding="ascii")
        self._ca_key = pki.load_key(self.home / "ca.key")

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

    def invited(self, body: dict, source: str) -> dict | str:
        """A machine joins with an invitation: paired at once. It proves it
        holds the token by a MAC over its own request (the token never
        travels); the answer proves the CA back the same way."""
        name = str(body.get("name", "")).strip().lower()
        key_kind = str(body.get("key_kind", "file"))
        if key_kind not in CLAIMABLE_KEY_KINDS:
            return (f"ERROR: key kind '{key_kind}' cannot be proven to this hub yet — "
                    "pair with a file key.")
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
        existing = registry.get(name)
        if existing and existing["status"] == "active":
            # Checked before redeeming: a refused join keeps its invitation.
            return f"ERROR: machine '{name}' is already paired — revoke it first."
        invitation = registry.redeem_invitation(
            str(token_id), name, str(body.get("csr", "")).encode("utf-8"),
            str(body.get("mac", "")))
        if isinstance(invitation, str):
            return invitation
        refused = policy(invitation["role"], key_kind)
        if refused:
            return refused
        cert_pem, fingerprint, serial, expires = self._issue(csr.public_key(), name)
        enrolled = registry.enroll(name, invitation["role"], key_kind, fingerprint, serial,
                                   expires, platform)
        if enrolled.startswith("ERROR"):
            return enrolled
        self._announce(f"machine '{name}' paired as {invitation['role']} by invitation "
                       f"of {invitation['created_by']} (from {source})")
        return {**common, "status": "approved", "name": name, "certificate": cert_pem,
                "proof": pki.invitation_proof(invitation["key"], self.ca_cert)}

    def commit(self, body: dict, source: str) -> dict | str:
        """Pairing by code, step 1: the machine commits to its request."""
        name = str(body.get("name", "")).strip().lower()
        key_kind = str(body.get("key_kind", "file"))
        if key_kind not in CLAIMABLE_KEY_KINDS:
            return (f"ERROR: key kind '{key_kind}' cannot be proven to this hub yet — "
                    "pair with a file key.")
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
        words = pki.check_words(self.ca_cert, csr.public_key(), nonce_joiner,
                                waiting["nonce_hub"])
        return registry.reveal_request(device_code, csr_text, words)

    def poll(self, body: dict) -> dict:
        return registry.poll_request(str(body.get("device_code", "")))

    # -- admin actions (bus listener, admin machines only) ------------------

    def lookup(self, user_code: str) -> dict | str:
        found = registry.lookup_request(user_code)
        if found is None:
            return (f"ERROR: no pending pairing request with code "
                    f"{registry.normalize_code(user_code)} (mistyped, expired or decided).")
        return found

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
        self._announce(f"machine '{final}' paired as {role}, approved by {approver} "
                       f"(request from {found['source']})")
        return {"result": done, "name": final, "expires_at": expires}

    def deny(self, user_code: str, approver: str) -> str:
        return registry.deny_request(user_code, approver)

    def invite(self, name: str, role: str, ttl: int, admin: str) -> dict | str:
        refused = policy(role, "file")
        if refused:
            return refused
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

    def machines(self) -> list[dict]:
        return registry.list_machines()

    def renew(self, machine: dict, peer_der: bytes) -> dict | str:
        """A fresh certificate for the key the machine just proved it holds
        (mutual TLS): no request to sign, the key never changes — which is
        also what a TPM-held key needs."""
        current = pki.load_cert_der(peer_der)
        cert_pem, fingerprint, serial, expires = self._issue(current.public_key(),
                                                              machine["name"])
        done = registry.renew(machine["name"], fingerprint, serial, expires)
        if done.startswith("ERROR"):
            return done
        return {"certificate": cert_pem, "expires_at": expires}


def load(home: Path) -> Authority:
    config = json.loads((Path(home) / "hub.json").read_text(encoding="utf-8"))
    return Authority(home, config)
