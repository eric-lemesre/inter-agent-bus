"""Tests of the multi-machine hub — needs the `[hub]` extra (cryptography),
not the MCP SDK. A real hub runs in a thread on the loopback interface;
the client side goes through the access layer exactly like the CLI, the
MCP server and the drivers do.

    python3 hub_test.py
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
from pathlib import Path

fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global fail
    print(("ok — " if ok else "FAIL: ") + label + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        fail = 1


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    for var in ("IAB_DB", "ORCHESTRATOR_DB", "IAB_HUB", "HTTPS_PROXY", "https_proxy"):
        os.environ.pop(var, None)
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-alpha")
    import access
    import hub
    import pki
    import registry
    import remote
    import store

    home = tmp / "hub"
    port = free_port()
    out = hub.init(["localhost", "127.0.0.1"], "alpha", home, port=port)
    check("hub init creates the hub and pairs the first admin", out.startswith("OK"), out)
    check("the CA key is owner-only",
          os.name != "posix" or (home / "ca.key").stat().st_mode & 0o077 == 0)
    check("the machine key is owner-only",
          os.name != "posix" or (tmp / "machine-alpha" / "machine.key").stat().st_mode & 0o077 == 0)
    again = hub.init(["localhost"], "alpha", home, port=port)
    check("hub init refuses to replace an existing CA", again.startswith("ERROR"), again)
    hub_db = os.environ["IAB_DB"]

    server, _ = hub.serve_in_thread(home, ("127.0.0.1", port))
    url = f"https://127.0.0.1:{port}"
    os.environ["IAB_HUB"] = url
    bus = access.bus

    check("IAB_HUB selects the remote backend", isinstance(access.backend(), remote.RemoteBackend))

    # Round trip of the task lifecycle through the hub.
    r = bus.register_agent("kimi@lab", "remote worker", cwd="/srv/lab")
    check("register through the hub", r.startswith("OK"), r)
    r = bus.push_task("kimi@lab", "h1", "payload ✓ " + "x" * 50_000, 5)
    check("push a large payload through the hub", r.startswith("OK"), r)
    claimed = json.loads(bus.claim_task("kimi@lab", 60))
    check("claim through the hub returns the payload intact",
          claimed["payload"] == "payload ✓ " + "x" * 50_000 and claimed["attempts"] == 1)
    r = bus.publish_result("kimi@lab", "h1", "done", attempt=claimed["attempts"])
    check("publish through the hub", r.startswith("OK"), r)
    check("read the result through the hub",
          json.loads(bus.read_result("h1"))["content"] == "done")
    os.environ.pop("IAB_HUB")
    os.environ["IAB_DB"] = hub_db
    check("the state lives in the hub's database",
          json.loads(store.read_result("h1"))["author"] == "kimi@lab")
    os.environ["IAB_HUB"] = url
    r = bus.push_task("ghost", "h0", "x")
    check("core refusals come back as is", r.startswith("ERROR") and "not registered" in r, r)
    info = json.loads(bus.db_info())
    check("db_info names the hub and the machine",
          info["db_source"] == "hub" and info["hub"] == url and info["machine"] == "alpha", info)
    check("non-string operations keep their type", bus.wake_count("kimi@lab", 3600) == 0)
    bus.touch_identity({"agent_name": "claude@lab", "cwd": "/home/x/lab", "model": "m"})
    check("touch_identity registers a remote client",
          any(a["agent"] == "claude@lab" for a in json.loads(bus.list_presence())))

    # Long-poll: a wait is served by the hub and woken by a push.
    def later_push() -> None:
        time.sleep(0.5)
        bus.push_task("kimi@lab", "h2", "x")

    threading.Thread(target=later_push).start()
    started = time.monotonic()
    snap = json.loads(bus.wait_signal("kimi@lab", "task", 10, 0.2))
    check("a remote wait wakes on a push", snap["signalled"] and "h2" in snap["tasks"]
          and time.monotonic() - started < 5, snap)
    store_cancel = bus.cancel_task("h2")

    # A long wait is chained in bounded rounds with the same deadline.
    remote.LONG_POLL_SECONDS = 1
    hub.LONG_POLL_SECONDS = 1
    started = time.monotonic()
    out = bus.wait_for_task("kimi@lab", 2.5, 0.2)
    elapsed = time.monotonic() - started
    check("a long remote wait is chained to its full deadline",
          out == "[]" and 2.3 < elapsed < 5, f"{out} {elapsed:.2f}s")
    remote.LONG_POLL_SECONDS = hub.LONG_POLL_SECONDS = 50

    # Transport refusals.
    status, answer = access.backend().request("POST", "/v1/op/resolve_identity", {"args": {}})
    check("a non-operation route is refused", status == 404, answer)
    status, answer = access.backend().request("POST", "/v1/op/push_task",
                                              {"args": {"bogus": 1}})
    check("bad arguments are refused with an ERROR line",
          status == 400 and answer["error"].startswith("ERROR"), answer)
    server.max_body = 1000
    r = bus.push_task("kimi@lab", "h3", "y" * 5000)
    check("an oversized request is refused, never truncated",
          r.startswith("ERROR") and "refused" in r, r)
    server.max_body = hub.MAX_BODY

    # Without a client certificate the hub does not answer.
    ctx = ssl.create_default_context(cafile=str(home / "ca.pem"))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                tls.sendall(b"POST /v1/op/get_system_state HTTP/1.1\r\nHost: x\r\n"
                            b"Content-Length: 2\r\n\r\n{}")
                answered = tls.recv(100)
    except (ssl.SSLError, OSError):
        answered = b""
    check("a client without a machine certificate gets nothing", answered == b"", answered)

    # TLS inspection: a server whose certificate does not chain to the bus
    # CA. The client must refuse during the handshake and send nothing.
    fake_ca_key, fake_ca = pki.new_ca("proxy")
    fake_key, fake_cert = pki.server_cert(fake_ca_key, fake_ca, ["127.0.0.1"])
    pki.write_private(tmp / "fake.key", pki.key_pem(fake_key))
    pki.write_public(tmp / "fake.pem", pki.cert_pem(fake_cert))
    fake_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    fake_ctx.load_cert_chain(str(tmp / "fake.pem"), str(tmp / "fake.key"))
    received: list[bytes] = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    fake_port = listener.getsockname()[1]

    def fake_proxy() -> None:
        conn, _ = listener.accept()
        try:
            with fake_ctx.wrap_socket(conn, server_side=True) as tls:
                tls.settimeout(3)
                received.append(tls.recv(65536))
        except (ssl.SSLError, OSError):
            pass

    threading.Thread(target=fake_proxy, daemon=True).start()
    os.environ["IAB_HUB"] = f"https://127.0.0.1:{fake_port}"
    r = bus.push_task("kimi@lab", "secret", "confidential payload")
    check("an intercepting proxy is detected", r.startswith("ERROR") and "TLS inspection" in r, r)
    time.sleep(0.2)
    check("nothing reached the intercepting proxy", not any(received), received)
    listener.close()
    os.environ["IAB_HUB"] = url

    # Revocation takes effect at the next request.
    second = tmp / "machine-beta"
    key = pki.new_key()
    cert = pki.machine_cert(pki.load_key(home / "ca.key"), pki.load_cert(home / "ca.pem"),
                            key.public_key(), "beta")
    os.environ.pop("IAB_HUB")
    os.environ["IAB_DB"] = hub_db
    registry.enroll("beta", "member", "file", pki.cert_sha256(pki.cert_der(cert)),
                    format(cert.serial_number, "x"), pki.not_after(cert).isoformat())
    hub.save_machine(second, pki.key_pem(key), pki.cert_pem(cert),
                     (home / "ca.pem").read_bytes(), {"name": "beta", "hub": url})
    os.environ["IAB_HUB"] = url
    os.environ["IAB_MACHINE_DIR"] = str(second)
    check("a second machine reaches the hub", bus.list_presence().startswith("["))
    import http.client
    kept = http.client.HTTPSConnection("127.0.0.1", port, timeout=10,
                                       context=remote.client_context(remote.credentials(second)))
    kept.request("GET", "/v1/ping")
    first = kept.getresponse()
    first.read()
    registry.revoke("beta")
    kept.request("GET", "/v1/ping")
    after = kept.getresponse()
    after.read()
    kept.close()
    check("a keep-alive connection does not outlive a revocation",
          (first.status, after.status) == (200, 403), f"{first.status} {after.status}")
    r = bus.list_presence()
    check("a revoked machine is refused at its next request",
          r.startswith("ERROR") and "no longer" in r, r)
    try:
        bus.wake_count("kimi@lab")
        raised = False
    except remote.HubError:
        raised = True
    check("a refused non-string operation raises HubError", raised)

    # A machine that never paired, and a hub that is down.
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "nowhere")
    r = bus.list_presence()
    check("an unpaired machine is told to join", r.startswith("ERROR") and "iab join" in r, r)
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-alpha")
    server.shutdown()
    server.server_close()
    r = bus.list_presence()
    check("a hub that is down is a loud failure, never a local fallback",
          r.startswith("ERROR") and "unreachable" in r, r)
    os.environ["IAB_HUB"] = "http://127.0.0.1:1"
    r = bus.list_presence()
    check("a non-https hub URL is refused", r.startswith("ERROR") and "https" in r, r)

if fail:
    print("hub_test: FAIL.")
    sys.exit(1)
print("hub_test: OK.")
