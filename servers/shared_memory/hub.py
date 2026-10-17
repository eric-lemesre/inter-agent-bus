"""Hub of the multi-machine bus — store.py over HTTPS with mutual TLS.

`iab hub init` creates the hub's home (bus CA, certificates,
configuration, database) and pairs the local machine as the first admin;
`iab hub serve` runs two listeners:

  - the BUS listener (mutual TLS): one route per bus operation
    (`POST /v1/op/<name>`, JSON arguments, the operation's return value as
    is — `ERROR:` lines included), plus certificate renewal and the admin
    routes (pairing approval, invitations, machines, revocation);
  - the PAIRING listener (no client certificate): a new machine asks to
    join and polls for the outcome — RFC 8628 device authorization. It is
    restricted to private source addresses unless told otherwise
    (owner's decision: no pairing from the Internet at first).

The hub is a thin wrapper like the MCP server: operations run in
store.py, machines and pairing requests in registry.py, certificate
issuance in pairing.py.

Security (the study's § 7): every bus request must come with a machine
certificate signed by the bus CA AND listed active in the registry,
checked at every request (revocation takes effect at once, keep-alive
included); payloads never reach the log, only the operation, the
machine, the status and the duration; a request larger than the limit
is refused, never truncated.

Hub home (IAB_HUB_HOME, default <data dir>/hub):
    ca.key ca.pem            bus CA (the key never leaves this directory)
    server.key server.pem    certificate of the bus listener (bus CA)
    pair.key pair.pem        certificate of the pairing listener — replace
                             with a public one (pair_cert/pair_key in
                             hub.json) when the hub has a public name
    hub.json                 names, ports, database, pairing policy
    bus.db                   the bus database (default)
"""
from __future__ import annotations

import inspect
import ipaddress
import json
import os
import platform
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # installed package (`iab` maps to this directory)
    from . import authz, pairing, pki, registry, remote, store
    from .access import OPERATIONS
except ImportError:  # bare script usage
    import authz
    import pairing
    import pki
    import registry
    import remote
    import store
    from access import OPERATIONS

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PAIR_PORT = remote.DEFAULT_PORT + 1
MAX_BODY = 32 * 1024 * 1024
MAX_PAIR_BODY = 64 * 1024
# A blocking wait is served for at most this long; the client chains waits.
LONG_POLL_SECONDS = remote.LONG_POLL_SECONDS
SERVER_RENEW_BEFORE_DAYS = 60
# Requests per machine (token bucket), configurable as "rate" in hub.json;
# requests per source address on the pairing listener ("pair_rate").
RATE_PER_SECOND = 50
RATE_BURST = 200
PAIR_RATE_PER_SECOND = 1
PAIR_RATE_BURST = 10
# Open connections (each holds a thread), checked before the TLS handshake.
MAX_CONNECTIONS = 256
MAX_CONNECTIONS_PER_SOURCE = 32
# A handshake must complete quickly; a request may then take longer.
HANDSHAKE_SECONDS = 10
SERVER_CERT_CHECK_SECONDS = 12 * 3600
_WAIT_TIMEOUT = {"wait_signal": "timeout_s", "wait_for_task": "timeout_s"}
# Where a pairing request may come from by default: loopback, private
# networks, CGNAT (a tailnet), link-local — never the open Internet.
PRIVATE_NETWORKS = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                    "100.64.0.0/10", "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10")


def hub_home() -> Path:
    return Path(os.environ.get("IAB_HUB_HOME") or store.DATA_DIR / "hub")


def _paths(home: Path) -> dict:
    return {
        "ca_key": home / "ca.key", "ca": home / "ca.pem",
        "server_key": home / "server.key", "server": home / "server.pem",
        "pair_key": home / "pair.key", "pair": home / "pair.pem",
        "config": home / "hub.json",
    }


def load_config(home: Path) -> dict:
    try:
        return json.loads(_paths(home)["config"].read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"no hub initialized in {home} ({exc.strerror}) — "
                           "run `iab hub init`") from exc
    except ValueError as exc:
        raise RuntimeError(f"{_paths(home)['config']} is not valid JSON: {exc}") from exc


def _log(message: str) -> None:
    print(f"[iab hub] {message}", file=sys.stderr, flush=True)


def _use_database(config: dict) -> None:
    # The hub IS the local side of the bus: store.py resolves its database
    # from IAB_DB, set here once for the whole process.
    os.environ["IAB_DB"] = str(config["db"])


def hub_url(config: dict) -> str:
    host, port = config["names"][0], int(config["port"])
    return f"https://{host}" + ("" if port == 443 else f":{port}")


def save_machine(directory: Path, key_pem: bytes, cert_pem: bytes, ca_pem: bytes,
                 info: dict) -> None:
    """Write a machine's credentials: key owner-only, the CA it will trust
    for the hub, and what it knows of its hub."""
    creds = remote.credentials(directory)
    pki.write_private(creds["key"], key_pem)
    pki.write_public(creds["cert"], cert_pem)
    pki.write_public(creds["ca"], ca_pem)
    remote.write_info(directory, info)


def init(names: list[str], machine: str, home: Path | None = None,
         db: str | None = None, port: int = remote.DEFAULT_PORT,
         pair_port: int | None = None) -> str:
    """Create the hub and pair this machine as its first admin. Refused on
    an existing hub (its CA would be replaced, every machine unpaired)."""
    home = home or hub_home()
    paths = _paths(home)
    names = [n.strip().lower() for n in names if n.strip()]
    machine = machine.strip().lower()
    if not names:
        return "ERROR: give at least one --name the machines will reach the hub by."
    if not registry.valid_machine_name(machine):
        return f"ERROR: invalid machine name '{machine}'."
    if paths["ca_key"].exists() or paths["config"].exists():
        return f"ERROR: a hub already exists in {home} — refusing to replace its CA."
    ca_key, ca_cert = pki.new_ca(names[0])
    pki.write_private(paths["ca_key"], pki.key_pem(ca_key))
    pki.write_public(paths["ca"], pki.cert_pem(ca_cert))
    for which in ("server", "pair"):
        key, cert = pki.server_cert(ca_key, ca_cert, names)
        pki.write_private(paths[f"{which}_key"], pki.key_pem(key))
        pki.write_public(paths[which], pki.cert_pem(cert))
    config = {
        "names": names, "port": port,
        "pair_port": pair_port if pair_port is not None else port + 1,
        "pair_allow": list(PRIVATE_NETWORKS),
        "db": db or str(home / "bus.db"),
    }
    # Private: the database target may be a URL carrying credentials.
    pki.write_private(paths["config"],
                      json.dumps(config, indent=2, ensure_ascii=False).encode("utf-8"))
    if os.name == "posix":
        os.chmod(home, 0o700)
    _use_database(config)
    # The first admin: this machine, issued directly (local access to the
    # hub's home is the proof of authority that pairing provides elsewhere).
    key = pki.new_key()
    cert = pki.machine_cert(ca_key, ca_cert, key.public_key(), machine)
    expires = pki.not_after(cert).isoformat()
    enrolled = registry.enroll(
        machine, "admin", "file", pki.cert_sha256(pki.cert_der(cert)),
        format(cert.serial_number, "x"), expires, _platform())
    if enrolled.startswith("ERROR"):
        return enrolled
    url = hub_url(config)
    save_machine(remote.machine_dir(), pki.key_pem(key), pki.cert_pem(cert),
                 pki.cert_pem(ca_cert), {"name": machine, "hub": url, "expires_at": expires})
    if os.name == "posix":
        os.chmod(remote.machine_dir(), 0o700)
    return (f"OK: hub created in {home}; machine '{machine}' paired as admin.\n"
            f"Start it with `iab hub serve`, then point clients at it: IAB_HUB={url}")


def import_bus(source: str, bind: str | None = None, dry_run: bool = False,
               home: Path | None = None) -> str:
    """Bring a machine's local bus into the hub's database (run on the hub:
    local access to its home is the authority, as for init). Imported
    instances belong to no machine — refused until an admin binds them —
    unless `bind` names the machine they come from: they are then bound to
    it, in the same transaction."""
    home = home or hub_home()
    try:
        config = load_config(home)
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    _use_database(config)
    after = None
    if bind:
        machine = bind.strip().lower()
        if (registry.get(machine) or {}).get("status") != "active":
            return f"ERROR: no active machine '{machine}' to bind the imported instances to."

        def after(conn, agents):
            for agent in agents:
                refused = registry.pin_in(conn, agent, machine, "hub import")
                if refused:
                    return refused
            return None
    out = store.import_database(source, dry_run=dry_run, after=after)
    if out.startswith("ERROR"):
        return out
    summary = json.loads(out)
    counts = ", ".join(f"{n} {table}" for table, n in summary["imported"].items())
    verb = "would import" if dry_run else "imported"
    lines = [f"OK: {verb} {counts} from {summary['source']}."]
    if summary["left_behind"]["wake_locks"]:
        lines.append(f"{summary['left_behind']['wake_locks']} wake lock(s) left behind "
                     "(held by the old machine's supervisor).")
    if summary["agents"] and bind:
        lines.append(f"Instances bound to '{bind.strip().lower()}': "
                     + ", ".join(summary["agents"]))
    elif summary["agents"]:
        lines.append("Bound to no machine (refused until then): "
                     + ", ".join(summary["agents"])
                     + " — `iab bind <instance> <machine>` from an admin machine, "
                       "or import again with --bind.")
    return "\n".join(lines)


def _platform() -> str:
    return f"{platform.system()} {platform.machine()}"


def _renew_server_cert(home: Path, config: dict) -> bool:
    """Renew the bus listener's certificate when it nears its end. True
    when it was renewed (a running server then reloads it)."""
    paths = _paths(home)
    cert = pki.load_cert(paths["server"])
    if not pki.needs_renewal(cert, SERVER_RENEW_BEFORE_DAYS):
        return False
    key, cert = pki.server_cert(pki.load_key(paths["ca_key"]), pki.load_cert(paths["ca"]),
                                config["names"])
    pki.write_private(paths["server_key"], pki.key_pem(key))
    pki.write_public(paths["server"], pki.cert_pem(cert))
    _log(f"server certificate renewed until {pki.not_after(cert).isoformat()}")
    return True


def _keep_server_cert_fresh(home: Path, config: dict, server: "HubServer") -> None:
    """A hub may run for more than a year: check the certificate twice a
    day and load a renewed one into the live context (new connections get
    it; established ones finish with the old)."""
    paths = _paths(home)
    while True:
        time.sleep(SERVER_CERT_CHECK_SECONDS)
        try:
            if _renew_server_cert(home, config):
                server.context.load_cert_chain(str(paths["server"]), str(paths["server_key"]))
        except Exception as exc:  # retried at the next check, said loudly
            _log(f"ERROR: server certificate renewal failed: {type(exc).__name__}: {exc}")


def server_context(home: Path) -> ssl.SSLContext:
    """Bus listener: TLS 1.3, a machine certificate signed by the bus CA
    required from every client."""
    paths = _paths(home)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(str(paths["server"]), str(paths["server_key"]))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(paths["ca"]))
    return ctx


def pair_context(home: Path, config: dict) -> ssl.SSLContext:
    """Pairing listener: TLS 1.2+, no client certificate (the machine has
    none yet). The public certificate configured in hub.json when there is
    one, else the hub's own (then only the check words authenticate)."""
    paths = _paths(home)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    cert = config.get("pair_cert") or str(paths["pair"])
    key = config.get("pair_key") or str(paths["pair_key"])
    ctx.load_cert_chain(cert, key)
    return ctx


def _bad(status: int, message: str) -> tuple[int, dict]:
    return status, {"error": message if message.startswith("ERROR") else f"ERROR: {message}"}


def _json_object(body: bytes) -> dict | None:
    try:
        value = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# Operations of the access layer the hub does not serve: where the hub
# keeps its database is none of a client's business.
_NOT_SERVED = frozenset({"db_info"})


def dispatch(name: str, body: bytes, machine: dict, caller: str = "",
             policy: authz.Authz | None = None) -> tuple[int, dict]:
    """Run one bus operation for an authenticated machine, after the
    authorization rules (authz.py) — `caller` is the instance the client
    says it is (checked against the machine's bindings there)."""
    if name not in OPERATIONS or name in _NOT_SERVED:
        return _bad(404, f"'{name}' is not a bus operation.")
    request = _json_object(body)
    if request is None:
        return _bad(400, "request body is not a JSON object.")
    arguments = request.get("args", {})
    if not isinstance(arguments, dict):
        return _bad(400, "'args' must be a JSON object.")
    function = getattr(store, name)
    try:
        bound = inspect.signature(function).bind(**arguments)
    except TypeError as exc:
        return _bad(400, f"bad arguments for {name}: {exc}.")
    bound.apply_defaults()  # the rules see what the operation will see
    key = _WAIT_TIMEOUT.get(name)
    if key is not None:
        bound.arguments[key] = max(0.0, min(float(bound.arguments[key]), LONG_POLL_SECONDS))
    policy = policy or authz.Authz()
    refused, reserved = policy.check(name, dict(bound.arguments), machine, caller)
    if refused:
        return _bad(403, refused)
    try:
        result = function(*bound.args, **bound.kwargs)
    except Exception:
        policy.after(name, dict(bound.arguments), "ERROR: internal", machine, reserved)
        raise
    policy.after(name, dict(bound.arguments), result, machine, reserved)
    return 200, {"result": result}


def _answer(result) -> tuple[int, dict]:
    """Map an authority answer (dict/list = success, ERROR line = refusal)."""
    if isinstance(result, str) and result.startswith("ERROR"):
        return _bad(409, result)
    return 200, {"result": result}


def admin_dispatch(action: str, body: bytes, machine: dict,
                   authority: pairing.Authority) -> tuple[int, dict]:
    if machine["role"] != "admin":
        return _bad(403, "this action needs an admin machine.")
    request = _json_object(body)
    if request is None:
        return _bad(400, "request body is not a JSON object.")
    who = machine["name"]
    if action == "pairing/lookup":
        return _answer(authority.lookup(str(request.get("user_code", ""))))
    if action == "pairing/approve":
        return _answer(authority.approve(str(request.get("user_code", "")),
                                         request.get("name") or None,
                                         str(request.get("role", "member")), who))
    if action == "pairing/deny":
        return _answer(authority.deny(str(request.get("user_code", "")), who))
    if action == "invite":
        try:
            ttl = int(request.get("ttl", registry.INVITATION_TTL))
        except (TypeError, ValueError):
            return _bad(400, "ttl must be a number of seconds.")
        if not 60 <= ttl <= 86400:
            return _bad(400, "ttl must be between 60 seconds and one day.")
        return _answer(authority.invite(str(request.get("name", "")),
                                        str(request.get("role", "member")), ttl, who))
    if action == "machines":
        return _answer(authority.machines())
    if action == "revoke":
        return _answer(authority.revoke(str(request.get("name", "")), who))
    if action == "bind":
        return _answer(registry.pin(str(request.get("agent", "")),
                                    str(request.get("machine", "")), who))
    return _bad(404, f"unknown admin action '{action}'.")


class _BaseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "iab-hub"
    sys_version = ""
    timeout = 60  # per socket operation: a stalled client frees its thread
    max_body = MAX_BODY

    def setup(self) -> None:
        super().setup()
        self.machine: dict | None = None
        self.fingerprint = ""
        self.peer_der = b""
        try:
            self.request.settimeout(HANDSHAKE_SECONDS)
            self.request.do_handshake()
            self.request.settimeout(self.timeout)
        except (ssl.SSLError, OSError) as exc:
            self.close_connection = True
            self.handshake_error = str(exc)
            # One line per refused handshake, source address included: the
            # hook for fail2ban or CrowdSec on a hub reachable from outside.
            _log(f"- handshake refused from {self.client_address[0]}: "
                 f"{getattr(exc, 'reason', None) or exc}")
            return
        self.handshake_error = ""
        self.peer_der = self.request.getpeercert(binary_form=True) or b""
        self.fingerprint = pki.cert_sha256(self.peer_der) if self.peer_der else ""

    def handle_one_request(self) -> None:
        if self.handshake_error:
            self.close_connection = True
            return
        super().handle_one_request()

    def _send(self, status: int, answer) -> None:
        data = json.dumps(answer, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes | None:
        limit = self.server.max_body
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            self._send(*_bad(413, f"request body exceeds {limit} bytes — "
                                  "refused, never truncated."))
            self.close_connection = True
            return None
        return self.rfile.read(length)

    def log_message(self, format: str, *args) -> None:
        # The stdlib access log is replaced by one line per request written
        # by the handlers, without payloads.
        pass


class _BusHandler(_BaseHandler):
    def _authenticate(self) -> None:
        # Per REQUEST, not per connection: a keep-alive connection opened
        # before a revocation must not outlive it.
        self.machine = registry.authenticate(self.fingerprint) if self.fingerprint else None

    def _route(self, body: bytes) -> tuple[int, dict]:
        if self.path.startswith("/v1/op/"):
            return dispatch(self.path[len("/v1/op/"):], body, self.machine,
                            self.headers.get("X-IAB-Caller", ""), self.server.authz)
        if self.path.startswith("/v1/admin/"):
            return admin_dispatch(self.path[len("/v1/admin/"):], body, self.machine,
                                  self.server.authority)
        if self.path == "/v1/renew":
            return _answer(self.server.authority.renew(self.machine, self.peer_der))
        return _bad(404, f"unknown route {self.path}.")

    def do_POST(self) -> None:
        started = time.monotonic()
        self._authenticate()
        if self.machine is None:
            status, answer = _bad(403, "this machine certificate is not (or no longer) "
                                       "paired with the hub.")
            self.close_connection = True
            self._send(status, answer)
        elif not self.server.admit(self.machine["name"]):
            status, answer = _bad(429, f"too many requests from machine "
                                       f"'{self.machine['name']}' — slow down.")
            self._send(status, answer)
        else:
            body = self._body()
            if body is None:
                status = 413
            else:
                # Events written while serving this request name its machine.
                stamp = store.CALLER_MACHINE.set(self.machine["name"])
                try:
                    status, answer = self._route(body)
                except Exception as exc:  # report, keep serving
                    _log(f"{self.machine['name']} {self.path}: internal error "
                         f"{type(exc).__name__}")
                    status, answer = _bad(500, "hub internal error.")
                finally:
                    store.CALLER_MACHINE.reset(stamp)
                self._send(status, answer)
        who = self.machine["name"] if self.machine else "-"
        route = registry.printable(self.path.removeprefix("/v1/op/"), 120)
        _log(f"{who} {route} {status} {time.monotonic() - started:.3f}s")

    def do_GET(self) -> None:
        self._authenticate()
        if self.machine is not None and not self.server.admit(self.machine["name"]):
            self._send(*_bad(429, "too many requests — slow down."))
            return
        if self.machine is None:
            self.close_connection = True
            self._send(*_bad(403, "this machine certificate is not paired with the hub."))
            return
        if self.path == "/v1/ping":
            self._send(200, {"result": {"machine": self.machine["name"],
                                        "role": self.machine["role"],
                                        "expires_at": self.machine["expires_at"],
                                        "hub": self.server.names[0]}})
            return
        self._send(*_bad(404, f"unknown route {self.path}."))


class _PairHandler(_BaseHandler):
    def _allowed(self) -> bool:
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        return any(address in network for network in self.server.pair_allow)

    def do_POST(self) -> None:
        started = time.monotonic()
        source = self.client_address[0]
        if not self._allowed():
            status, answer = _bad(403, "pairing is not open from this network.")
            self.close_connection = True
            self._send(status, answer)
        elif not self.server.admit(source):
            # Unauthenticated: each request may take the writer lock of the
            # bus, so it is rationed per source address.
            status, answer = _bad(429, "too many pairing requests — slow down.")
            self.close_connection = True
            self._send(status, answer)
        else:
            body = self._body()
            request = _json_object(body) if body is not None else None
            if body is None:
                status = 413
            elif request is None:
                status, answer = _bad(400, "request body is not a JSON object.")
                self._send(status, answer)
            else:
                authority = self.server.authority
                try:
                    if self.path == "/v1/pair/commit":
                        status, answer = _answer(authority.commit(request, source))
                    elif self.path == "/v1/pair/reveal":
                        status, answer = _answer(authority.reveal(request))
                    elif self.path == "/v1/pair/invited":
                        status, answer = _answer(authority.invited(request, source))
                    elif self.path == "/v1/pair/poll":
                        status, answer = 200, {"result": self.server.authority.poll(request)}
                    else:
                        status, answer = _bad(404, f"unknown route {self.path}.")
                except Exception as exc:  # report, keep serving
                    _log(f"pairing {self.path}: internal error {type(exc).__name__}")
                    status, answer = _bad(500, "hub internal error.")
                self._send(status, answer)
        _log(f"pairing {registry.printable(self.path, 120)} from {source} {status} "
             f"{time.monotonic() - started:.3f}s")


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, context: ssl.SSLContext,
                 authority: pairing.Authority, max_body: int = MAX_BODY,
                 pair_allow: tuple = ()) -> None:
        self.context = context
        self.authority = authority
        self.names = authority.config["names"]
        self.max_body = max_body
        self.pair_allow = pair_allow
        self.authz = authz.Authz(authority.config.get("policy"))
        pairing_listener = handler is _PairHandler
        limits = authority.config.get("pair_rate" if pairing_listener else "rate") or {}
        self.rate = float(limits.get(
            "per_second", PAIR_RATE_PER_SECOND if pairing_listener else RATE_PER_SECOND))
        self.burst = float(limits.get(
            "burst", PAIR_RATE_BURST if pairing_listener else RATE_BURST))
        self._buckets: dict[str, tuple[float, float]] = {}
        self._bucket_lock = threading.Lock()
        self._open: dict[str, int] = {}
        self._open_total = 0
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, handler)

    def admit(self, machine: str) -> bool:
        """Token bucket per machine: a runaway agent on one machine cannot
        starve the others (a refused request says so, never waits)."""
        now = time.monotonic()
        with self._bucket_lock:
            tokens, last = self._buckets.get(machine, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._buckets[machine] = (tokens, now)
                return False
            self._buckets[machine] = (tokens - 1, now)
            return True

    def _source_allowed(self, source: str) -> bool:
        if not self.pair_allow:
            return True
        try:
            address = ipaddress.ip_address(source)
        except ValueError:
            return False
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        return any(address in network for network in self.pair_allow)

    def verify_request(self, request, client_address) -> bool:
        """Before any TLS work, in the accept loop: the pairing listener's
        network filter, and a cap on open connections (each one holds a
        thread) — overall and per source address. A refused connection is
        closed at once."""
        source = client_address[0]
        if not self._source_allowed(source):
            return False
        with self._bucket_lock:
            if (self._open_total >= MAX_CONNECTIONS
                    or self._open.get(source, 0) >= MAX_CONNECTIONS_PER_SOURCE):
                refused = True
            else:
                refused = False
                self._open_total += 1
                self._open[source] = self._open.get(source, 0) + 1
        if refused:
            _log(f"- connection from {source} refused: too many open connections")
        return not refused

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            source = client_address[0]
            with self._bucket_lock:
                self._open_total -= 1
                left = self._open.get(source, 1) - 1
                if left:
                    self._open[source] = left
                else:
                    self._open.pop(source, None)

    def handle_error(self, request, client_address) -> None:
        # A client hanging up mid-exchange is routine on a network service:
        # one line, no traceback (and never the request's content).
        kind = sys.exc_info()[0]
        _log(f"- connection from {client_address[0]} ended: "
             f"{kind.__name__ if kind else 'error'}")

    def get_request(self):
        sock, address = self.socket.accept()
        # The TLS handshake runs in the request's thread (setup), not here:
        # a slow or hostile client must not stall the accept loop.
        return self.context.wrap_socket(sock, server_side=True,
                                        do_handshake_on_connect=False), address


def make_servers(home: Path | None = None, listen: tuple[str, int] | None = None,
                 pair_listen: tuple[str, int] | None | bool = None,
                 max_body: int = MAX_BODY) -> tuple[HubServer, HubServer | None]:
    """The bus listener and the pairing listener (None when pairing is
    off: `pair_listen=False` or `pair_port: 0` in hub.json)."""
    home = home or hub_home()
    config = load_config(home)
    _use_database(config)
    _renew_server_cert(home, config)
    authority = pairing.Authority(home, config)
    address = listen or (DEFAULT_HOST, int(config.get("port", remote.DEFAULT_PORT)))
    bus = HubServer(address, _BusHandler, server_context(home), authority, max_body)
    pair_port = int(config.get("pair_port", DEFAULT_PAIR_PORT))
    if pair_listen is False or (pair_listen is None and not pair_port):
        return bus, None
    networks = tuple(ipaddress.ip_network(n) for n in config.get("pair_allow",
                                                                 PRIVATE_NETWORKS))
    pair_address = pair_listen if isinstance(pair_listen, tuple) else (address[0], pair_port)
    pair = HubServer(pair_address, _PairHandler, pair_context(home, config), authority,
                     MAX_PAIR_BODY, networks)
    return bus, pair


def serve(home: Path | None = None, listen: tuple[str, int] | None = None,
          pair_listen: tuple[str, int] | None | bool = None) -> int:
    try:
        bus, pair = make_servers(home, listen, pair_listen)
    except (RuntimeError, OSError, ssl.SSLError, ValueError) as exc:
        _log(f"ERROR: {exc}")
        return 1
    threading.Thread(target=_keep_server_cert_fresh,
                     args=(home or hub_home(), load_config(home or hub_home()), bus),
                     daemon=True).start()
    host, port = bus.server_address[:2]
    _log(f"serving the bus on {host}:{port} "
         f"(database {store.dialect.describe(os.environ['IAB_DB'])})")
    if pair is not None:
        phost, pport = pair.server_address[:2]
        _log(f"pairing open on {phost}:{pport} from {', '.join(map(str, pair.pair_allow))}")
        threading.Thread(target=pair.serve_forever, daemon=True).start()
    else:
        _log("pairing listener off")
    try:
        bus.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bus.server_close()
        if pair is not None:
            pair.shutdown()
            pair.server_close()
    return 0


def serve_in_thread(home: Path, listen: tuple[str, int],
                    pair_listen: tuple[str, int] | None | bool = False
                    ) -> tuple[HubServer, HubServer | None]:
    """A hub in background threads of this process — tests and smoke."""
    bus, pair = make_servers(home, listen, pair_listen)
    for server in (bus, pair):
        if server is not None:
            threading.Thread(target=server.serve_forever, daemon=True).start()
    return bus, pair


def parse_listen(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"expected HOST:PORT (got '{text}')")
    return host.strip("[]") or DEFAULT_HOST, int(port)
