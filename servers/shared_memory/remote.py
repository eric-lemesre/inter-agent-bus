"""Remote backend of the inter-agent bus — the client of a hub.

Selected by IAB_HUB (`https://host[:port]`, see access.py). Every bus
operation becomes one HTTPS request to the hub, authenticated by the
machine certificate obtained at pairing (mutual TLS):

  - the hub's certificate is checked against the bus CA ONLY (pinned in
    the machine directory at pairing), never against the system store: a
    TLS-inspecting proxy fails the handshake and nothing is sent;
  - one connection per call — no keep-alive whose silent drop would force
    a retry of a non-idempotent operation (a push, a claim);
  - an unreachable hub is a loud failure, never a fallback on a local
    database (that would be a partition, worse than an outage);
  - blocking waits (wait_signal, wait_for_task) are split into bounded
    long-polls, so a proxy idle timeout never cuts a wait.

Standard library only (`ssl`, `http.client`): `cryptography` is needed
for pairing and renewal (pki.py), not for calling the hub.
"""
from __future__ import annotations

import http.client
import inspect
import json
import os
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

try:  # installed package (`iab` maps to this directory)
    from . import store
    from .access import OPERATIONS
except ImportError:  # bare script usage
    import store
    from access import OPERATIONS

DEFAULT_PORT = 7443
# Upper bound of one long-poll round trip; the hub caps waits the same way.
LONG_POLL_SECONDS = 50
CALL_TIMEOUT = 30  # seconds, any non-waiting call
# Operations that do not return a string: a failure raises HubError
# instead of returning an `ERROR:` line their callers would not read.
_NON_STRING = frozenset({"touch_presence", "touch_identity", "record_wake", "wake_count"})
_WAITS = frozenset({"wait_signal", "wait_for_task"})
# A machine certificate (30 days) is renewed when less than this remains;
# renewal needs the hub, so a machine offline longer must pair again.
RENEW_BEFORE_SECONDS = 10 * 86400
_LOCK_STALE_SECONDS = 120


class HubError(RuntimeError):
    """The hub could not serve an operation (transport or refusal)."""


def machine_dir() -> Path:
    """Where this machine keeps its key, certificate and the pinned bus CA."""
    return Path(os.environ.get("IAB_MACHINE_DIR") or store.DATA_DIR / "machine")


def credentials(directory: Path | None = None) -> dict:
    directory = directory or machine_dir()
    return {
        "dir": directory,
        "key": directory / "machine.key",
        "cert": directory / "machine.pem",
        "ca": directory / "ca.pem",
        "info": directory / "machine.json",
    }


def machine_info(directory: Path | None = None) -> dict:
    try:
        return json.loads(credentials(directory)["info"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_info(directory: Path, info: dict) -> None:
    path = credentials(directory)["info"]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _replace_file(path: Path, data: bytes) -> None:
    """Swap a file atomically: a concurrent reader sees the old or the new
    certificate, never half of one."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def parse_hub(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(url if "://" in url else f"https://{url}")
    if parsed.scheme != "https" or not parsed.hostname:
        raise HubError(f"IAB_HUB must be an https URL (got '{url}')")
    return parsed.hostname, parsed.port or DEFAULT_PORT


def client_context(creds: dict) -> ssl.SSLContext:
    """Mutual TLS, bus CA only, hostname checked, TLS 1.3."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(creds["ca"]))
    ctx.load_cert_chain(str(creds["cert"]), str(creds["key"]))
    return ctx


def open_connection(host: str, port: int, ctx: ssl.SSLContext,
                    timeout: float) -> http.client.HTTPSConnection:
    """HTTPS connection to the hub, through HTTPS_PROXY by a CONNECT tunnel
    when the environment asks for one (the proxy sees only ciphertext)."""
    proxy = urllib.request.getproxies().get("https")
    if proxy and not urllib.request.proxy_bypass(host):
        parsed = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 3128,
                                           context=ctx, timeout=timeout)
        conn.set_tunnel(host, port)
        return conn
    return http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)


def describe_failure(url: str, exc: BaseException) -> str:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return (f"ERROR: the hub at {url} presented a certificate not signed by the bus "
                f"CA ({exc.verify_message}) — TLS inspection or wrong host; nothing was "
                "sent.")
    if isinstance(exc, ssl.SSLError):
        return (f"ERROR: TLS handshake with the hub at {url} failed ({exc.reason}) — "
                "certificate refused (revoked or expired?) or TLS inspection.")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"ERROR: hub unreachable at {url}: timed out."
    return f"ERROR: hub unreachable at {url}: {exc}."


class RemoteBackend:
    """Bus operations served by a hub: `backend.push_task(...)` has the
    signature of store.push_task and returns what it would return."""

    def __init__(self, url: str, directory: Path | None = None) -> None:
        self.url = url
        self.creds = credentials(directory)
        # Who this process is (its environment and launch directory): the
        # hub counts it as a principal only if bound to this machine.
        self.caller = store.resolve_identity().get("agent_name") or ""
        try:
            self.host, self.port = parse_hub(url)
            self.error = ""
        except HubError as exc:  # reported at the first call, like any failure
            self.host, self.port, self.error = "", 0, f"ERROR: {exc}"

    def __getattr__(self, name: str):
        if name not in OPERATIONS:
            raise AttributeError(f"'{name}' is not a bus operation")
        if name == "db_info":
            return self._db_info
        return partial(self._call, name)

    # -- transport ---------------------------------------------------------

    def _context(self) -> ssl.SSLContext:
        if self.error:
            raise HubError(self.error)
        missing = [k for k in ("key", "cert", "ca") if not self.creds[k].is_file()]
        if missing:
            raise HubError(
                f"ERROR: this machine is not paired with a hub (no {', '.join(missing)} "
                f"in {self.creds['dir']}) — run `iab join`.")
        return client_context(self.creds)

    def request(self, method: str, path: str, body: dict | None = None,
                timeout: float = CALL_TIMEOUT) -> tuple[int, dict]:
        """One HTTPS exchange. Raises HubError on any transport failure,
        before or after sending."""
        try:
            ctx = self._context()
        except HubError:
            raise
        except (OSError, ssl.SSLError) as exc:
            raise HubError(f"ERROR: machine credentials unusable in "
                           f"{self.creds['dir']}: {exc}.") from exc
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        conn = open_connection(self.host, self.port, ctx, timeout)
        try:
            conn.request(method, path, body=data,
                         headers={"Content-Type": "application/json",
                                  "X-IAB-Caller": self.caller})
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise HubError(describe_failure(self.url, exc)) from exc
        finally:
            conn.close()
        try:
            return status, json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise HubError(f"ERROR: the hub at {self.url} answered HTTP {status} with a "
                           "body that is not JSON.") from exc

    def _op(self, name: str, arguments: dict, timeout: float = CALL_TIMEOUT):
        status, answer = self.request("POST", f"/v1/op/{name}", {"args": arguments},
                                      timeout=timeout)
        if status != 200 or "result" not in answer:
            raise HubError(str(answer.get("error") or f"ERROR: hub answered HTTP {status}."))
        return answer["result"]

    # -- operations --------------------------------------------------------

    def renew(self, force: bool = False) -> str:
        """Renew this machine's certificate when it nears its end (or now,
        with force). Serialized across processes by a lock file; the new
        certificate replaces the old one atomically."""
        info = machine_info(self.creds["dir"])
        expires = info.get("expires_at")
        if not force and expires:
            left = (datetime.fromisoformat(expires) - datetime.now(timezone.utc)).total_seconds()
            if left > RENEW_BEFORE_SECONDS:
                return f"OK: certificate valid until {expires} — no renewal needed."
        lock = self.creds["dir"] / "renew.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _LOCK_STALE_SECONDS:
                    lock.unlink()
            except OSError:
                pass
            return "OK: renewal already in progress in another process."
        os.close(fd)
        try:
            if not force and machine_info(self.creds["dir"]).get("expires_at") != expires:
                return "OK: renewed meanwhile by another process."
            status, answer = self.request("POST", "/v1/renew", {})
            if status != 200:
                return str(answer.get("error") or f"ERROR: hub answered HTTP {status}.")
            result = answer["result"]
            _replace_file(self.creds["cert"], result["certificate"].encode("ascii"))
            write_info(self.creds["dir"], {**info, "expires_at": result["expires_at"]})
            return f"OK: certificate renewed until {result['expires_at']}."
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    def _maybe_renew(self) -> None:
        # Opportunistic, never blocking the operation: a failed renewal is
        # retried at the next call and the current certificate still works.
        if not self.creds["cert"].is_file():
            return  # not paired: the operation itself reports it
        try:
            outcome = self.renew()
        except (HubError, OSError):
            return
        if outcome.startswith("ERROR"):
            print(f"[iab] certificate renewal failed: {outcome}", file=sys.stderr)

    def _call(self, name: str, *args, **kwargs):
        try:
            arguments = inspect.signature(getattr(store, name)).bind(*args, **kwargs).arguments
        except TypeError as exc:
            raise TypeError(f"{name}: {exc}") from None
        self._maybe_renew()
        try:
            if name in _WAITS:
                return self._wait(name, dict(arguments))
            return self._op(name, dict(arguments))
        except HubError as exc:
            if name in _NON_STRING:
                raise
            return str(exc)

    def _wait(self, name: str, arguments: dict) -> str:
        """A long wait as a chain of bounded long-polls with the same overall
        deadline; the last answer (a timeout snapshot) is returned as is."""
        total = float(arguments.get("timeout_s", 300.0))
        deadline = time.monotonic() + max(0.0, total)
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            arguments["timeout_s"] = min(remaining, LONG_POLL_SECONDS)
            result = self._op(name, arguments, timeout=arguments["timeout_s"] + CALL_TIMEOUT)
            if not isinstance(result, str) or result.startswith("ERROR"):
                return result
            if name == "wait_for_task":
                if result != "[]":
                    return result
            elif json.loads(result).get("signalled"):
                return result
            if deadline - time.monotonic() <= 0:
                return result

    def _db_info(self) -> str:
        """The rendezvous seen from this machine: the hub and this machine's
        pairing (where the hub keeps its database is not disclosed)."""
        info = {"db": None, "db_source": "hub", "hub": self.url,
                "machine": machine_info(self.creds["dir"]).get("name")}
        try:
            status, answer = self.request("GET", "/v1/ping")
            if status == 200:
                info["role"] = answer["result"]["role"]
            else:
                info["error"] = str(answer.get("error") or f"ERROR: hub answered HTTP {status}.")
        except HubError as exc:
            info["error"] = str(exc)
        return json.dumps(info, ensure_ascii=False)


_BACKENDS: dict[tuple[str, str], RemoteBackend] = {}


def backend_for(url: str) -> RemoteBackend:
    key = (url, str(machine_dir()))
    if key not in _BACKENDS:
        _BACKENDS[key] = RemoteBackend(url)
    return _BACKENDS[key]
