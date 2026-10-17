"""Hub of the multi-machine bus — store.py over HTTPS with mutual TLS.

`iab hub init` creates the hub's home (bus CA, server certificate,
configuration, database) and pairs the local machine as the first admin;
`iab hub serve` runs the bus listener. The hub is a thin wrapper like the
MCP server: one route per bus operation (`POST /v1/op/<name>`, JSON
arguments, the operation's return value as is — `ERROR:` lines included).
No state logic lives here: operations run in store.py, machines in
registry.py.

Security (the study's § 7): every connection must present a machine
certificate signed by the bus CA AND listed active in the registry
(revocation takes effect at the next request); payloads never reach the
log, only the operation, the machine, the status and the duration; a
request larger than the limit is refused, never truncated.

Hub home (IAB_HUB_HOME, default <data dir>/hub):
    ca.key ca.pem            bus CA (the key never leaves this directory)
    server.key server.pem    certificate of the bus listener
    hub.json                 names, database, listener
    bus.db                   the bus database (default)
"""
from __future__ import annotations

import inspect
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
    from . import pki, registry, remote, store
    from .access import OPERATIONS
except ImportError:  # bare script usage
    import pki
    import registry
    import remote
    import store
    from access import OPERATIONS

DEFAULT_LISTEN = ("0.0.0.0", remote.DEFAULT_PORT)
MAX_BODY = 32 * 1024 * 1024
# A blocking wait is served for at most this long; the client chains waits.
LONG_POLL_SECONDS = remote.LONG_POLL_SECONDS
SERVER_RENEW_BEFORE_DAYS = 60
_WAIT_TIMEOUT = {"wait_signal": "timeout_s", "wait_for_task": "timeout_s"}


def hub_home() -> Path:
    return Path(os.environ.get("IAB_HUB_HOME") or store.DATA_DIR / "hub")


def _paths(home: Path) -> dict:
    return {
        "ca_key": home / "ca.key", "ca": home / "ca.pem",
        "server_key": home / "server.key", "server": home / "server.pem",
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
    pki.write_public(creds["info"],
                     json.dumps(info, indent=2, ensure_ascii=False).encode("utf-8"))


def init(names: list[str], machine: str, home: Path | None = None,
         db: str | None = None, port: int = remote.DEFAULT_PORT) -> str:
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
    server_key, server_cert = pki.server_cert(ca_key, ca_cert, names)
    pki.write_private(paths["server_key"], pki.key_pem(server_key))
    pki.write_public(paths["server"], pki.cert_pem(server_cert))
    config = {"names": names, "port": port, "db": db or str(home / "bus.db")}
    pki.write_public(paths["config"],
                     json.dumps(config, indent=2, ensure_ascii=False).encode("utf-8"))
    if os.name == "posix":
        os.chmod(home, 0o700)
    _use_database(config)
    # The first admin: this machine, issued directly (local access to the
    # hub's home is the proof of authority that pairing provides elsewhere).
    key = pki.new_key()
    cert = pki.machine_cert(ca_key, ca_cert, key.public_key(), machine)
    der = pki.cert_der(cert)
    enrolled = registry.enroll(
        machine, "admin", "file", pki.cert_sha256(der), format(cert.serial_number, "x"),
        pki.not_after(cert).isoformat(), _platform())
    if enrolled.startswith("ERROR"):
        return enrolled
    url = hub_url(config)
    save_machine(remote.machine_dir(), pki.key_pem(key), pki.cert_pem(cert),
                 pki.cert_pem(ca_cert), {"name": machine, "hub": url})
    return (f"OK: hub created in {home}; machine '{machine}' paired as admin.\n"
            f"Start it with `iab hub serve`, then point clients at it: IAB_HUB={url}")


def _platform() -> str:
    return f"{platform.system()} {platform.machine()}"


def _renew_server_cert(home: Path, config: dict) -> None:
    paths = _paths(home)
    cert = pki.load_cert(paths["server"])
    if not pki.needs_renewal(cert, SERVER_RENEW_BEFORE_DAYS):
        return
    key, cert = pki.server_cert(pki.load_key(paths["ca_key"]), pki.load_cert(paths["ca"]),
                                config["names"])
    pki.write_private(paths["server_key"], pki.key_pem(key))
    pki.write_public(paths["server"], pki.cert_pem(cert))
    _log(f"server certificate renewed until {pki.not_after(cert).isoformat()}")


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


def _bad(status: int, message: str) -> tuple[int, dict]:
    return status, {"error": message if message.startswith("ERROR") else f"ERROR: {message}"}


def dispatch(name: str, body: bytes, machine: dict) -> tuple[int, dict]:
    """Run one bus operation for an authenticated machine."""
    if name not in OPERATIONS:
        return _bad(404, f"'{name}' is not a bus operation.")
    try:
        request = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return _bad(400, "request body is not JSON.")
    arguments = request.get("args", {}) if isinstance(request, dict) else None
    if not isinstance(arguments, dict):
        return _bad(400, "'args' must be a JSON object.")
    function = getattr(store, name)
    try:
        bound = inspect.signature(function).bind(**arguments)
    except TypeError as exc:
        return _bad(400, f"bad arguments for {name}: {exc}.")
    key = _WAIT_TIMEOUT.get(name)
    if key is not None:
        requested = bound.arguments.get(key, inspect.signature(function).parameters[key].default)
        bound.arguments[key] = max(0.0, min(float(requested), LONG_POLL_SECONDS))
    return 200, {"result": function(*bound.args, **bound.kwargs)}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "iab-hub"
    sys_version = ""
    timeout = 60  # per socket operation: a stalled client frees its thread

    def setup(self) -> None:
        super().setup()
        self.machine: dict | None = None
        try:
            self.request.do_handshake()
        except (ssl.SSLError, OSError) as exc:
            self.close_connection = True
            self.handshake_error = str(exc)
            # One line per refused handshake, source address included: the
            # hook for fail2ban or CrowdSec on a hub reachable from outside.
            _log(f"- handshake refused from {self.client_address[0]}: "
                 f"{getattr(exc, 'reason', None) or exc}")
            return
        self.handshake_error = ""
        der = self.request.getpeercert(binary_form=True)
        if der:
            self.machine = registry.authenticate(pki.cert_sha256(der))

    def _send(self, status: int, answer: dict) -> None:
        data = json.dumps(answer, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_one_request(self) -> None:
        if self.handshake_error:
            self.close_connection = True
            return
        super().handle_one_request()

    def _body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > self.server.max_body:
            self._send(*_bad(413, f"request body exceeds {self.server.max_body} bytes — "
                                  "refused, never truncated."))
            self.close_connection = True
            return None
        return self.rfile.read(length)

    def do_POST(self) -> None:
        started = time.monotonic()
        name = self.path[len("/v1/op/"):] if self.path.startswith("/v1/op/") else ""
        if self.machine is None:
            status, answer = _bad(403, "this machine certificate is not (or no longer) "
                                       "paired with the hub.")
            self.close_connection = True
            self._send(status, answer)
        elif not name:
            status, answer = _bad(404, f"unknown route {self.path}.")
            self._send(status, answer)
        else:
            body = self._body()
            if body is None:
                status = 413
            else:
                try:
                    status, answer = dispatch(name, body, self.machine)
                except Exception as exc:  # the operation failed: report, keep serving
                    _log(f"{self.machine['name']} {name}: internal error "
                         f"{type(exc).__name__}")
                    status, answer = _bad(500, f"hub internal error in {name}.")
                self._send(status, answer)
        who = self.machine["name"] if self.machine else "-"
        _log(f"{who} {name or self.path} {status} {time.monotonic() - started:.3f}s")

    def do_GET(self) -> None:
        if self.machine is None:
            self.close_connection = True
            self._send(*_bad(403, "this machine certificate is not paired with the hub."))
            return
        if self.path == "/v1/ping":
            self._send(200, {"result": {"machine": self.machine["name"],
                                        "role": self.machine["role"],
                                        "hub": self.server.names[0]}})
            return
        self._send(*_bad(404, f"unknown route {self.path}."))

    def log_message(self, format: str, *args) -> None:
        # The stdlib access log would print request lines only — still, one
        # log line per request is written by do_POST, without payloads.
        pass


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, context: ssl.SSLContext, names: list[str],
                 max_body: int = MAX_BODY) -> None:
        self.context = context
        self.names = names
        self.max_body = max_body
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.address_family = family
        super().__init__(address, _Handler)

    def get_request(self):
        sock, address = self.socket.accept()
        # The TLS handshake runs in the request's thread (setup), not here:
        # a slow or hostile client must not stall the accept loop.
        return self.context.wrap_socket(sock, server_side=True,
                                        do_handshake_on_connect=False), address


def make_server(home: Path | None = None, listen: tuple[str, int] | None = None,
                max_body: int = MAX_BODY) -> HubServer:
    home = home or hub_home()
    config = load_config(home)
    _use_database(config)
    _renew_server_cert(home, config)
    address = listen or (DEFAULT_LISTEN[0], int(config.get("port", remote.DEFAULT_PORT)))
    return HubServer(address, server_context(home), config["names"], max_body)


def serve(home: Path | None = None, listen: tuple[str, int] | None = None) -> int:
    try:
        server = make_server(home, listen)
    except (RuntimeError, OSError, ssl.SSLError) as exc:
        _log(f"ERROR: {exc}")
        return 1
    host, port = server.server_address[:2]
    _log(f"serving the bus on {host}:{port} (database {os.environ['IAB_DB']})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def serve_in_thread(home: Path, listen: tuple[str, int]) -> tuple[HubServer, threading.Thread]:
    """A hub in a background thread of this process — tests and smoke."""
    server = make_server(home, listen)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def parse_listen(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"--listen expects HOST:PORT (got '{text}')")
    return host.strip("[]") or DEFAULT_LISTEN[0], int(port)
