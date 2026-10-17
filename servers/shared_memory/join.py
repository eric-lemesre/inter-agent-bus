"""Pairing driver of the multi-machine bus — `iab join`, on the new machine.

The machine generates its key (it never leaves the machine), sends a
certificate request to the hub's pairing listener and either:

  - shows a code and four check words, then waits for a person to type
    the code on an admin machine (`iab approve`) and confirm the words —
    the Plex/Netflix flow, RFC 8628; or
  - with an invitation token (Ansible), is paired at once.

The first contact may happen over a channel this machine cannot verify
(the hub has no public certificate): the check words, bound to both the
bus CA and this machine's key, authenticate it; with a token, the hub's
proof over the CA does. Once paired, the machine trusts the bus CA only.

Process orchestration only: state lives on the hub (registry.py).
"""
from __future__ import annotations

import hmac
import http.client
import json
import os
import platform
import re
import secrets
import ssl
import time
from pathlib import Path

try:  # installed package (`iab` maps to this directory)
    from . import pki, registry, remote
except ImportError:  # bare script usage
    import pki
    import registry
    import remote

DEFAULT_PAIR_PORT = remote.DEFAULT_PORT + 1
POLL_TIMEOUT = 15


class JoinError(RuntimeError):
    pass


def _target(hub: str) -> tuple[str, int]:
    text = hub if "://" in hub else f"https://{hub}"
    host, port = remote.parse_hub(text)
    explicit = ":" in text.split("://", 1)[1].split("/", 1)[0].rsplit("]", 1)[-1]
    return host, port if explicit else DEFAULT_PAIR_PORT


class _Channel:
    """HTTPS to the pairing listener. Verified against the system's trust
    store when the hub has a public certificate; otherwise unverified, said
    loudly — the check words (or the invitation proof) carry the trust."""

    def __init__(self, host: str, port: int, say) -> None:
        self.host, self.port, self.say = host, port, say
        self.verified = True

    def _context(self) -> ssl.SSLContext:
        if self.verified:
            return ssl.create_default_context()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        for _ in range(2):
            conn = remote.open_connection(self.host, self.port, self._context(), POLL_TIMEOUT)
            try:
                conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                             headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                status, raw = response.status, response.read()
            except ssl.SSLCertVerificationError:
                if not self.verified:
                    raise
                self.verified = False
                self.say("The hub's pairing certificate is not recognised by this system: "
                         "the check words below are the only protection of this pairing — "
                         "compare them carefully.")
                continue
            except (OSError, http.client.HTTPException) as exc:
                raise JoinError(f"cannot reach the hub's pairing listener at "
                                f"{self.host}:{self.port}: {exc}") from exc
            finally:
                conn.close()
            try:
                return status, json.loads(raw.decode("utf-8"))
            except ValueError as exc:
                raise JoinError(f"the pairing listener answered HTTP {status} with a body "
                                "that is not JSON") from exc
        raise JoinError("unreachable")  # pragma: no cover


_CODE_RE = re.compile(r"[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}")


def _clean(text, limit: int = 300) -> str:
    # Everything the pairing listener sends may come from a party in the
    # middle (unverified first contact): nothing it says is printed raw.
    return registry.printable(text, limit)


def _result(status: int, answer: dict) -> dict:
    if status != 200 or "result" not in answer:
        raise JoinError(_clean(answer.get("error") or f"hub answered HTTP {status}")
                        .removeprefix("ERROR: "))
    result = answer["result"]
    if not isinstance(result, dict):
        raise JoinError("the pairing listener sent an unexpected answer")
    return result


def _seconds(value, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        raise JoinError("the pairing listener sent an unexpected answer") from None


def _check_certificate(cert_pem: str, ca_cert, key, name: str):
    cert = pki.load_cert_bytes(cert_pem.encode("ascii"))
    if not pki.issued_by(cert, ca_cert):
        raise JoinError("the certificate received is not signed by the hub's CA")
    if not pki.same_key(cert.public_key(), key.public_key()):
        raise JoinError("the certificate received is not for this machine's key")
    if pki.common_name(cert) != name:
        raise JoinError(f"the certificate received names '{_clean(pki.common_name(cert))}', "
                        f"not '{name}'")
    return cert


def _by_invitation(channel: "_Channel", body: dict, key, token: str) -> tuple[dict, str, str]:
    # The token itself never travels (the channel may be unverified): an
    # id to find the invitation, a MAC binding it to THIS request.
    body["invitation"] = registry.invitation_id(token)
    body["mac"] = registry.invitation_mac(registry.invitation_key(token),
                                          body["csr"].encode("utf-8"))
    answer = _result(*channel.post("/v1/pair/invited", body))
    ca_cert = pki.load_cert_bytes(str(answer.get("ca", "")).encode("ascii"))
    expected = pki.invitation_proof(registry.invitation_key(token), ca_cert)
    if not hmac.compare_digest(expected, str(answer.get("proof", ""))):
        raise JoinError("the hub could not prove it issued this invitation — nothing was saved")
    return answer, str(answer["name"]), str(answer["certificate"])


def _by_code(channel: "_Channel", body: dict, key, say,
             timeout: float | None) -> tuple[dict, str, str]:
    # Commit, then reveal (see pki.check_words): the hub's nonce is drawn
    # after this machine committed to its request, and this machine
    # reveals the request only after receiving that nonce and the CA.
    nonce = secrets.token_hex(32)
    csr_text = body.pop("csr")
    body["commitment"] = pki.commitment(csr_text.encode("utf-8"), nonce)
    answer = _result(*channel.post("/v1/pair/commit", body))
    ca_cert = pki.load_cert_bytes(str(answer.get("ca", "")).encode("ascii"))
    nonce_hub = str(answer.get("nonce_hub", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", nonce_hub):
        raise JoinError("the pairing listener sent an unexpected answer")
    device_code = str(answer.get("device_code", ""))
    revealed = _result(*channel.post("/v1/pair/reveal", {
        "device_code": device_code, "csr": csr_text, "nonce": nonce}))
    code = str(revealed.get("user_code", ""))
    if not _CODE_RE.fullmatch(code):
        raise JoinError("the pairing listener sent an unexpected code")
    expires_in = _seconds(revealed.get("expires_in"), 0, 3600)
    interval = _seconds(revealed.get("interval"), 1, 60)
    words = pki.check_words(ca_cert, key.public_key(), nonce, nonce_hub)
    say(f"Hub: {_clean(answer.get('hub', ''), 80)}"
        + ("" if channel.verified else " (certificate not verified)"))
    say("To pair this machine, on an admin machine run:")
    say("    iab approve")
    say(f"and type the code:   {code}   (valid {expires_in // 60} min, waiting…)")
    # Computed here, printed last on its own line: what the person compares.
    say(f"Check words: {words}")
    deadline = time.monotonic() + (timeout if timeout is not None else expires_in)
    while time.monotonic() < deadline:
        time.sleep(interval)
        status = _result(*channel.post("/v1/pair/poll", {"device_code": device_code}))
        state = status.get("status")
        if state == "approved":
            return answer, str(status["name"]), str(status["certificate"])
        if state == "slow_down":
            interval = min(60, interval + 5)
        elif state in ("denied", "expired"):
            raise JoinError(f"pairing {state}")
    raise JoinError("pairing not approved in time")


def join(hub: str, name: str | None = None, token: str | None = None,
         directory: Path | None = None, say=print, timeout: float | None = None) -> int:
    """Pair this machine with a hub. Returns a process exit code."""
    directory = directory or remote.machine_dir()
    creds = remote.credentials(directory)
    if creds["cert"].exists():
        say(f"ERROR: this machine is already paired ({creds['dir']}) — revoke it on the hub "
            "and remove that directory to pair again.")
        return 1
    name = (name or platform.node().split(".")[0]).strip().lower()
    if not registry.valid_machine_name(name):
        say(f"ERROR: invalid machine name '{name}' — pass --name.")
        return 1
    host, port = _target(hub)
    key = pki.new_key()
    body = {"name": name, "platform": f"{platform.system()} {platform.machine()}",
            "key_kind": "file",
            "csr": pki.csr_pem(pki.new_csr(key, name)).decode("ascii")}
    channel = _Channel(host, port, say)
    try:
        if token:
            answer, final, cert_pem = _by_invitation(channel, body, key, token)
        else:
            answer, final, cert_pem = _by_code(channel, body, key, say, timeout)
        if not registry.valid_machine_name(final):
            raise JoinError("the hub sent an invalid machine name")
        ca_cert = pki.load_cert_bytes(str(answer["ca"]).encode("ascii"))
        cert = _check_certificate(cert_pem, ca_cert, key, final)
        bus_url = str(answer.get("bus_url", ""))
        remote.parse_hub(bus_url)
    except (JoinError, remote.HubError, ValueError, KeyError) as exc:
        say(f"ERROR: {_clean(exc)}.")
        return 1
    expires = pki.not_after(cert).isoformat()
    write_credentials(directory, key, cert_pem, str(answer["ca"]),
                      {"name": final, "hub": bus_url, "expires_at": expires})
    say(f"Paired as '{final}'. Certificate valid until {expires[:10]}, renewed automatically.")
    say(f"Point this machine's clients at the hub: IAB_HUB={_clean(bus_url, 200)}")
    return 0


def write_credentials(directory: Path, key, cert_pem: str, ca_pem: str, info: dict) -> None:
    creds = remote.credentials(directory)
    pki.write_private(creds["key"], pki.key_pem(key))
    pki.write_public(creds["cert"], cert_pem.encode("ascii"))
    pki.write_public(creds["ca"], ca_pem.encode("ascii"))
    remote.write_info(directory, info)
    if os.name == "posix":
        os.chmod(directory, 0o700)


def read_token(value: str | None) -> str | None:
    """The invitation token: never on the command line (visible to every
    user in the process list) — from IAB_INVITE_TOKEN, or stdin with `-`."""
    if value == "-":
        import sys
        return sys.stdin.readline().strip() or None
    if value:
        raise JoinError("pass the token on stdin (`--token -`) or in IAB_INVITE_TOKEN, "
                        "never as an argument")
    return os.environ.get("IAB_INVITE_TOKEN") or None


def ping(say=print) -> int:
    """Check this machine's pairing against IAB_HUB (or the hub it was
    paired with)."""
    url = os.environ.get("IAB_HUB") or remote.machine_info().get("hub")
    if not url:
        say("ERROR: no hub — set IAB_HUB or pair with `iab join`.")
        return 1
    backend = remote.RemoteBackend(url)
    try:
        status, answer = backend.request("GET", "/v1/ping")
    except remote.HubError as exc:
        say(str(exc))
        return 1
    if status != 200:
        say(str(answer.get("error")))
        return 1
    info = answer["result"]
    say(json.dumps({"hub": url, **info}, ensure_ascii=False))
    return 0


