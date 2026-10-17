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
    import admin
    import hub
    import join
    import pki
    import registry
    import remote
    import store

    home = tmp / "hub"
    port, pair_port = free_port(), free_port()
    out = hub.init(["localhost", "127.0.0.1"], "alpha", home, port=port, pair_port=pair_port)
    check("hub init creates the hub and pairs the first admin", out.startswith("OK"), out)
    check("the CA key is owner-only",
          os.name != "posix" or (home / "ca.key").stat().st_mode & 0o077 == 0)
    check("the machine key is owner-only",
          os.name != "posix" or (tmp / "machine-alpha" / "machine.key").stat().st_mode & 0o077 == 0)
    again = hub.init(["localhost"], "alpha", home, port=port)
    check("hub init refuses to replace an existing CA", again.startswith("ERROR"), again)
    hub_db = os.environ["IAB_DB"]

    server, pair_server = hub.serve_in_thread(home, ("127.0.0.1", port),
                                              ("127.0.0.1", pair_port))
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

    # -- 9c: pairing by code (RFC 8628) and by invitation -------------------
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-alpha")
    registry.POLL_INTERVAL = 1
    pair_target = f"127.0.0.1:{pair_port}"

    def joining(name: str, directory: Path, lines: list, token: str | None = None,
                timeout: float = 30) -> dict:
        outcome: dict = {}

        def run() -> None:
            outcome["code"] = join.join(pair_target, name, token, directory,
                                        say=lines.append, timeout=timeout)

        thread = threading.Thread(target=run)
        thread.start()
        outcome["thread"] = thread
        return outcome

    def shown(lines: list, prefix: str) -> str:
        for _ in range(100):
            for line in list(lines):
                if prefix in line:
                    return line.split(prefix, 1)[1].split()[0]
            time.sleep(0.05)
        return ""

    lines: list[str] = []
    gamma_dir = tmp / "machine-gamma"
    pending = joining("gamma", gamma_dir, lines)
    code = shown(lines, "type the code:")
    words = shown(lines, "Check words:")
    check("join shows a code of two groups of four", len(code) == 9 and code[4] == "-", code)
    check("join shows four check words", len(words.split("-")) == 4, words)
    check("an unverified first contact is said loudly",
          any("not recognised" in line for line in lines), lines)
    asked: list[str] = []
    approver_lines: list[str] = []

    def confirm(prompt: str) -> str:
        asked.append(prompt)
        return "y" if f"Check words: {words}" in approver_lines else "n"

    check("an admin pairing a member with a file key is refused by policy",
          admin.call("pairing/approve", {"user_code": code, "role": "admin"})
          .startswith("ERROR") and "TPM" in admin.call(
              "pairing/approve", {"user_code": code, "role": "admin"}))
    rc = admin.approve(code.lower().replace("-", ""), None, "member", ask=confirm,
                       say=approver_lines.append)
    pending["thread"].join(30)
    check("the approver sees the same check words as the joining machine",
          any(words in line for line in approver_lines), approver_lines)
    check("approval with confirmed words pairs the machine",
          rc == 0 and pending.get("code") == 0, f"{rc} {pending.get('code')} {lines[-3:]}")
    os.environ["IAB_MACHINE_DIR"] = str(gamma_dir)
    check("the new machine reaches the bus", bus.list_presence().startswith("["))
    check("its key is owner-only",
          os.name != "posix" or (gamma_dir / "machine.key").stat().st_mode & 0o077 == 0)
    r = admin.call("machines")
    check("a member machine cannot use admin actions", str(r).startswith("ERROR")
          and "admin" in str(r), r)
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-alpha")
    names = {m["name"]: m for m in admin.call("machines")}
    check("the admin lists the paired machine",
          names.get("gamma", {}).get("role") == "member", names)
    announced = json.loads(bus.read_channel(since_seq=0, topic="machines"))
    check("the pairing is announced on the channel by `bus`",
          any("'gamma' paired" in e["message"] and e["author"] == "bus" for e in announced),
          announced)
    again = join.join(pair_target, "gamma", None, gamma_dir, say=lambda *_: None)
    check("join refuses an already paired machine", again == 1)

    # Words bind both keys: swapping the CA or the machine key changes them.
    other_ca = pki.new_ca("intruder")[1]
    some_key = pki.new_key().public_key()
    ca_cert = pki.load_cert(home / "ca.pem")
    check("check words change when the CA is swapped",
          pki.check_words(ca_cert, some_key) != pki.check_words(other_ca, some_key))
    check("check words change when the machine key is swapped",
          pki.check_words(ca_cert, some_key) != pki.check_words(ca_cert,
                                                               pki.new_key().public_key()))

    # A person who does not confirm the words denies the request.
    lines = []
    pending = joining("epsilon", tmp / "machine-epsilon", lines)
    code = shown(lines, "type the code:")
    rc = admin.approve(code, None, "member", ask=lambda _: "n", say=lambda *_: None)
    pending["thread"].join(30)
    check("unconfirmed words deny the pairing",
          rc == 1 and pending.get("code") == 1 and any("denied" in x for x in lines), lines)
    check("a denied machine got no credentials",
          not (tmp / "machine-epsilon" / "machine.pem").exists())
    r = admin.call("pairing/lookup", {"user_code": "BCDF-GHJK"})
    check("an unknown code is refused", str(r).startswith("ERROR"), r)

    # Invitation (Ansible): paired at once, single use, bound to its name.
    token = admin.call("invite", {"name": "delta", "role": "member"})["token"]
    lines = []
    rc = join.join(pair_target, "delta", token, tmp / "machine-delta", say=lines.append)
    check("an invitation pairs without a person", rc == 0, lines)
    rc = join.join(pair_target, "delta", token, tmp / "machine-delta2", say=lines.append)
    check("an invitation is single use", rc == 1 and "already" in lines[-1], lines[-1])
    token = admin.call("invite", {"name": "zeta", "role": "member"})["token"]
    lines = []
    rc = join.join(pair_target, "eta", token, tmp / "machine-eta", say=lines.append)
    check("an invitation is bound to its machine name", rc == 1, lines)
    forged = join._Channel("127.0.0.1", pair_port, lambda *_: None)
    k = pki.new_key()
    csr = pki.csr_pem(pki.new_csr(k, "zeta")).decode("ascii")
    status, answer = forged.post("/v1/pair/request", {
        "name": "zeta", "csr": csr, "invitation": registry.invitation_id(token),
        "mac": "00" * 32})
    check("an invitation id without the token's MAC is refused",
          status == 409 and "invitation" in answer["error"], answer)
    rc = join.join(pair_target, "zeta", token, tmp / "machine-zeta", say=lines.append)
    check("the refused attempts did not consume the invitation", rc == 0, lines[-2:])
    check("admin invitations are refused by policy (TPM required)",
          str(admin.call("invite", {"name": "theta", "role": "admin"})).startswith("ERROR"))

    # Pairing only from allowed networks; requests limited per source.
    saved_allow = pair_server.pair_allow
    pair_server.pair_allow = (hub.ipaddress.ip_network("10.0.0.0/8"),)
    status, answer = forged.post("/v1/pair/request", {"name": "iota", "csr": csr})
    check("pairing is refused from a network outside the allow list",
          status == 403 and "not open" in answer["error"], answer)
    pair_server.pair_allow = saved_allow
    saved_limit = registry.MAX_REQUESTS_PER_SOURCE
    registry.MAX_REQUESTS_PER_SOURCE = 1
    status, answer = forged.post("/v1/pair/request", {"name": "iota", "csr": csr})
    check("pairing requests are rate-limited per source",
          status == 409 and "slow down" in answer["error"], answer)
    registry.MAX_REQUESTS_PER_SOURCE = saved_limit

    # Renewal: not before it is due; forced, the old certificate is retired
    # at the first use of the new one.
    os.environ["IAB_MACHINE_DIR"] = str(gamma_dir)
    backend = access.backend()
    check("no renewal while the certificate is far from its end",
          "no renewal needed" in backend.renew())
    old_cert = (gamma_dir / "machine.pem").read_bytes()
    out = backend.renew(force=True)
    check("a forced renewal replaces the certificate",
          out.startswith("OK") and (gamma_dir / "machine.pem").read_bytes() != old_cert, out)
    stale = tmp / "machine-gamma-stale"
    stale.mkdir()
    for f in ("machine.key", "ca.pem", "machine.json"):
        (stale / f).write_bytes((gamma_dir / f).read_bytes())
    (stale / "machine.pem").write_bytes(old_cert)
    status_old = remote.RemoteBackend(url, stale).request("GET", "/v1/ping")[0]
    check("the previous certificate still works until the new one is used", status_old == 200)
    check("the renewed certificate works", bus.list_presence().startswith("["))
    status_old = remote.RemoteBackend(url, stale).request("GET", "/v1/ping")[0]
    check("the previous certificate is refused once the new one was used", status_old == 403)
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-alpha")
    r = admin.call("revoke", {"name": "alpha"})
    check("revoking the last admin is refused", str(r).startswith("ERROR"), r)
    r = admin.call("revoke", {"name": "delta"})
    check("an admin revokes a machine", str(r).startswith("OK"), r)
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-delta")
    check("the revoked machine is refused at once",
          bus.list_presence().startswith("ERROR"))

    # A machine that never paired, and a hub that is down.
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "nowhere")
    r = bus.list_presence()
    check("an unpaired machine is told to join", r.startswith("ERROR") and "iab join" in r, r)
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-alpha")
    pair_server.shutdown()
    pair_server.server_close()
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
