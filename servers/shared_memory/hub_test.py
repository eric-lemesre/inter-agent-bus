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
import sqlite3
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
    import authz
    import hub
    import join
    import pairing
    import pki
    import attest
    import registry
    import remote
    import softtpm
    import store

    home = tmp / "hub"
    port, pair_port = free_port(), free_port()
    from testdb import PG, fresh_db
    print(f"engine: {'postgresql' if PG else 'sqlite'}")
    out = hub.init(["localhost", "127.0.0.1"], "alpha", home,
                   db=fresh_db(tmp, "hub") if PG else None, port=port, pair_port=pair_port)
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
    # Every request of this file comes from 127.0.0.1: the pairing ration
    # is lifted here and checked on its own below.
    pair_server.rate, pair_server.burst = 1000, 1000
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
          info["db_source"] == "hub" and info["hub"] == url and info["machine"] == "alpha"
          and info["role"] == "admin" and "error" not in info, info)
    status, answer = access.backend().request("POST", "/v1/op/db_info", {"args": {}})
    check("the hub does not disclose where it keeps its database", status == 404, answer)
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
                timeout: float = 30, chip=None) -> dict:
        outcome: dict = {}

        def run() -> None:
            outcome["code"] = join.join(pair_target, name, token, directory,
                                        say=lines.append, timeout=timeout, chip=chip)

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
    nj, nh, at = "1" * 64, "2" * 64, "https://hub:7443"
    base_words = pki.check_words(ca_cert, some_key, nj, nh, at)
    check("check words change when the CA is swapped",
          base_words != pki.check_words(other_ca, some_key, nj, nh, at))
    check("check words change when the machine key is swapped",
          base_words != pki.check_words(ca_cert, pki.new_key().public_key(), nj, nh, at))
    check("check words change with either side's nonce",
          base_words != pki.check_words(ca_cert, some_key, "3" * 64, nh, at)
          and base_words != pki.check_words(ca_cert, some_key, nj, "4" * 64, at))
    check("check words change when the hub address is redirected",
          base_words != pki.check_words(ca_cert, some_key, nj, nh, "https://evil:7443"))
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    odd = pki.csr_pem(pki.new_csr(_ec.generate_private_key(_ec.SECP384R1()), "odd"))
    try:
        pki.load_csr(odd)
        odd_refused = False
    except ValueError:
        odd_refused = True
    check("a request with a key other than P-256 is refused", odd_refused)

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
    status, answer = forged.post("/v1/pair/invited", {
        "name": "zeta", "csr": csr, "invitation": registry.invitation_id(token),
        "mac": "00" * 32})
    check("an invitation id without the token's MAC is refused",
          status == 409 and "invitation" in answer["error"], answer)
    status, answer = forged.post("/v1/pair/invited", {
        "name": "gamma", "csr": csr, "invitation": registry.invitation_id(token),
        "mac": "00" * 32})
    check("without a valid invitation nobody learns which names are paired",
          status == 409 and "already paired" not in answer["error"], answer)
    rc = join.join(pair_target, "zeta", token, tmp / "machine-zeta", say=lines.append)
    check("the refused attempts did not consume the invitation", rc == 0, lines[-2:])
    # Commit-then-reveal: a reveal must match the commitment made before
    # the hub's nonce was known; displayed fields cannot drive a terminal.
    commitment = pki.commitment(csr.encode("ascii"), "ab" * 32)
    status, answer = forged.post("/v1/pair/commit", {
        "name": "kappa", "commitment": commitment,
        "platform": "Linux \x1b[2J\x1b[1;1HCheck words: faux\u202e"})
    committed = answer["result"]
    status, answer = forged.post("/v1/pair/reveal", {
        "device_code": committed["device_code"], "csr": csr, "nonce": "cd" * 32})
    check("a reveal that does not match its commitment is refused",
          status == 409 and "commitment" in answer["error"], answer)
    status, answer = forged.post("/v1/pair/reveal", {
        "device_code": committed["device_code"], "csr": csr, "nonce": "ab" * 32})
    check("a matching reveal yields a code to approve",
          status == 200 and len(answer["result"]["user_code"]) == 9, answer)
    seen = admin.call("pairing/lookup", {"user_code": answer["result"]["user_code"]})
    check("control and format characters never reach the approver",
          "\x1b" not in seen["platform"] and "\u202e" not in seen["platform"], seen)
    status, answer = forged.post("/v1/pair/reveal", {
        "device_code": committed["device_code"], "csr": csr, "nonce": "ab" * 32})
    check("a request is revealed once", status == 409, answer)
    admin.call("pairing/deny", {"user_code": seen["user_code"]})

    # -- 9e: TPM keys, attested (attest.py) — softtpm plays the chip --------
    token = admin.call("invite", {"name": "theta", "role": "admin"})["token"]
    lines = []
    rc = join.join(pair_target, "theta", token, tmp / "machine-theta", say=lines.append)
    check("an admin invitation is refused to a file key", rc == 1 and "TPM" in lines[-1],
          lines[-1:])
    soft = softtpm.SoftChip()
    fingerprint = pki.spki_sha256(soft.ek_key.public_key())
    lines = []
    rc = join.join(pair_target, "theta", token, tmp / "machine-theta", say=lines.append,
                   chip=soft)
    check("a TPM whose endorsement key nobody vouches for is refused, with the fingerprint "
          "to trust", rc == 1 and f"iab trust-ek {fingerprint}" in lines[-1], lines[-1:])
    check("join shows that fingerprint on the machine itself",
          any(fingerprint in line for line in lines[:2]), lines[:2])
    r = admin.call("tpm/trust", {"fingerprint": "abc"})
    check("a malformed EK fingerprint is refused", str(r).startswith("ERROR"), r)
    r = admin.call("tpm/trust", {"fingerprint": fingerprint.upper(), "note": "theta vTPM"})
    check("an admin trusts an endorsement key by its fingerprint", str(r).startswith("OK"), r)
    listed = admin.call("tpm/trusted")
    check("trusted endorsement keys are listed with who vouched",
          isinstance(listed, list) and [(e["fingerprint"], e["added_by"]) for e in listed]
          == [(fingerprint, "alpha")], listed)
    soft.answer = b"\0" * attest.SECRET_BYTES
    lines = []
    rc = join.join(pair_target, "theta", token, tmp / "machine-theta", say=lines.append,
                   chip=soft)
    check("a machine that does not return the hub's secret is refused",
          rc == 1 and "did not return" in lines[-1], lines[-1:])
    soft.answer = None
    lines = []
    rc = join.join(pair_target, "theta", token, tmp / "machine-theta", say=lines.append,
                   chip=soft)
    check("the refused attempts left the admin invitation usable: an attested TPM key "
          "redeems it", rc == 0, lines[-3:])
    theta = registry.get("theta") or {}
    check("the machine is recorded as an admin with an attested TPM key",
          theta.get("role") == "admin" and theta.get("key_kind") == "tpm"
          and "trusted by alpha" in theta.get("attestation", ""), theta)
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-theta")
    r = admin.call("machines")
    check("the attested admin machine can use admin actions", isinstance(r, list), r)
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-alpha")
    check("the machine list says how each key was proven",
          any(m["name"] == "theta" and m["attestation"] for m in r if isinstance(r, list)), r)
    announced = json.loads(bus.read_channel(since_seq=0, topic="machines"))
    check("the trusted EK and the TPM pairing are announced",
          any(fingerprint in e["message"] and "theta vTPM" in e["message"] for e in announced)
          and any("'theta' paired as admin (tpm key)" in e["message"] for e in announced),
          [e["message"] for e in announced][-3:])

    # The proof answers once, for the key it was made for.
    k = soft.new_key()
    csr_k = soft.csr(k, "iota")
    status, challenge = forged.post("/v1/pair/challenge", {
        "csr": csr_k, "tpm": {"public": attest.encode(k.public_area),
                              **soft.endorsement().material}})
    check("the hub answers a TPM with a challenge",
          status == 200 and "credential_blob" in challenge.get("result", {}), challenge)
    challenge = challenge["result"]
    proof = {"id": challenge["id"], "secret": attest.encode(soft.activate(k, None, challenge))}
    token = admin.call("invite", {"name": "iota", "role": "member"})["token"]

    def invited(csr_text: str, attestation) -> tuple[int, dict]:
        return forged.post("/v1/pair/invited", {
            "name": "iota", "key_kind": "tpm", "csr": csr_text, "attestation": attestation,
            "invitation": registry.invitation_id(token),
            "mac": registry.invitation_mac(registry.invitation_key(token),
                                           csr_text.encode("utf-8"))})

    status, answer = invited(pki.csr_pem(pki.new_csr(pki.new_key(), "iota")).decode("ascii"),
                             proof)
    check("a proof made for one key does not pair another",
          status == 409 and "another key" in answer["error"], answer)
    status, answer = invited(csr_k, proof)
    check("a proof answers once", status == 409 and "unknown or expired" in answer["error"],
          answer)
    status, answer = invited(csr_k, None)
    check("a TPM key without its proof is refused",
          status == 409 and "challenge" in answer["error"], answer)
    authority = pair_server.authority
    asked = [forged.post("/v1/pair/challenge", {
        "csr": csr_k, "tpm": {"public": attest.encode(k.public_area),
                              **soft.endorsement().material}})
        for _ in range(pairing.MAX_CHALLENGES_PER_SOURCE + 1)]
    check("one address holds a bounded number of challenges",
          asked[-1][0] == 409 and "from this address" in asked[-1][1]["error"]
          and all(a[0] == 200 for a in asked[:-1]), asked[-1])
    authority._challenges.clear()
    saved_max = pairing.MAX_CHALLENGES
    pairing.MAX_CHALLENGES = 3
    for n in range(3):
        authority._challenges[f"other{n}"] = (time.monotonic() + 60, b"", "", "", f"10.0.0.{n}")
    status, answer = forged.post("/v1/pair/challenge", {
        "csr": csr_k, "tpm": {"public": attest.encode(k.public_area),
                              **soft.endorsement().material}})
    check("a full challenge table gives way to a new machine, oldest first",
          status == 200 and "other0" not in authority._challenges
          and len(authority._challenges) == 3, answer)
    pairing.MAX_CHALLENGES = saved_max
    authority._challenges.clear()

    # By code: the approver sees how the key was proven and may make it admin.
    lines = []
    pending = joining("kappa", tmp / "machine-kappa", lines, chip=soft)
    code = shown(lines, "type the code:")
    words = shown(lines, "Check words:")
    approver_lines = []
    rc = admin.approve(code, None, "admin",
                       ask=lambda _: "y" if f"Check words: {words}" in approver_lines else "n",
                       say=approver_lines.append)
    pending["thread"].join(30)
    check("the approver is shown how the TPM key was proven",
          any("TPM key attested" in line and fingerprint in line for line in approver_lines),
          approver_lines)
    check("a TPM key paired by code may be approved as admin",
          rc == 0 and pending.get("code") == 0
          and (registry.get("kappa") or {}).get("role") == "admin", approver_lines[-2:])
    r = admin.call("tpm/untrust", {"fingerprint": fingerprint})
    check("an admin withdraws the trust of an endorsement key", str(r).startswith("OK"), r)
    token = admin.call("invite", {"name": "lambda", "role": "member"})["token"]
    lines = []
    rc = join.join(pair_target, "lambda", token, tmp / "machine-lambda", say=lines.append,
                   chip=soft)
    check("an endorsement key no longer trusted pairs no new machine",
          rc == 1 and "trust-ek" in lines[-1], lines[-1:])
    check("the machines it attested stay paired",
          (registry.get("kappa") or {}).get("status") == "active")
    for name in ("theta", "kappa"):  # alpha is the last admin again, as below expects
        admin.call("revoke", {"name": name})

    # Pairing only from allowed networks; requests limited per source.
    saved_allow = pair_server.pair_allow
    pair_server.pair_allow = (hub.ipaddress.ip_network("10.0.0.0/8"),)
    try:
        forged.post("/v1/pair/commit", {"name": "iota", "commitment": commitment})
        closed = False
    except join.JoinError:
        closed = True
    check("a network outside the allow list is closed before any TLS work", closed)
    pair_server.pair_allow = saved_allow

    # Pairing requests are rationed per source address (unauthenticated,
    # each may take the writer lock); polling an unknown code is cheap.
    saved_rate = (pair_server.rate, pair_server.burst)
    pair_server.rate, pair_server.burst = 0.001, 1
    pair_server._buckets.clear()
    first = forged.post("/v1/pair/poll", {"device_code": "nope"})
    second = forged.post("/v1/pair/poll", {"device_code": "nope"})
    check("an unknown pairing code is answered as expired",
          first[0] == 200 and first[1]["result"]["status"] == "expired", first)
    check("pairing requests beyond a source's budget are refused",
          second[0] == 429, second)
    pair_server.rate, pair_server.burst = saved_rate
    pair_server._buckets.clear()

    # Connections are capped per source before TLS, and a handshake that
    # does not complete is cut after HANDSHAKE_SECONDS.
    saved_cap, saved_hs = hub.MAX_CONNECTIONS_PER_SOURCE, hub.HANDSHAKE_SECONDS
    hub.MAX_CONNECTIONS_PER_SOURCE, hub.HANDSHAKE_SECONDS = 2, 1
    held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
    time.sleep(0.2)
    extra = socket.create_connection(("127.0.0.1", port), timeout=5)
    extra.settimeout(3)
    try:
        cut = extra.recv(1) == b""
    except OSError:
        cut = True
    check("a source over its connection cap is closed at once", cut)
    extra.close()
    started = time.monotonic()
    try:
        silent = held[0].recv(1) == b""
    except OSError:
        silent = True
    check("a silent client is cut after the handshake timeout",
          silent and time.monotonic() - started < 4, f"{time.monotonic() - started:.1f}s")
    for s in held:
        s.close()
    hub.MAX_CONNECTIONS_PER_SOURCE, hub.HANDSHAKE_SECONDS = saved_cap, saved_hs
    time.sleep(0.3)
    saved_limit = registry.MAX_REQUESTS_PER_SOURCE
    registry.MAX_REQUESTS_PER_SOURCE = 1
    status, answer = forged.post("/v1/pair/commit", {"name": "iota",
                                                     "commitment": commitment})
    check("pairing requests are rate-limited per source",
          status == 409 and "slow down" in answer["error"], answer)
    registry.MAX_REQUESTS_PER_SOURCE = saved_limit

    # Renewal: not before it is due; forced, the old certificate is retired
    # at the first use of the new one.
    os.environ["IAB_MACHINE_DIR"] = str(gamma_dir)
    backend = access.backend()
    check("no renewal while the certificate is far from its end",
          "no renewal needed" in backend.renew())
    # Two renewals whose answers are lost in a row: the certificate the
    # machine holds must still be accepted.
    gamma_der = pki.cert_der(pki.load_cert(gamma_dir / "machine.pem"))
    authority = server.authority
    for _ in range(2):
        authority.renew(registry.get("gamma"), gamma_der)  # answers never reach gamma
    check("two lost renewal answers in a row do not lock the machine out",
          bus.list_presence().startswith("["))
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

    # -- 9d: authorization -------------------------------------------------
    alpha_dir = str(tmp / "machine-alpha")
    os.environ["IAB_MACHINE_DIR"] = str(gamma_dir)
    r = bus.claim_task("kimi@lab", 60)
    check("another machine cannot claim an instance's queue",
          r.startswith("ERROR") and "belongs to machine 'alpha'" in r, r)
    for label, out in [("publish", bus.publish_result("kimi@lab", "h1", "forged")),
                       ("beat the heart", bus.heartbeat("kimi@lab")),
                       ("read the mailbox", bus.poll("kimi@lab")),
                       ("notify", bus.notify("kimi@lab", "claude@lab", "hi")),
                       ("take the wake lock", bus.acquire_wake_lock("kimi@lab"))]:
        check(f"another machine cannot {label} in an instance's name",
              out.startswith("ERROR") and "belongs to machine" in out, out)
    check("reads are not restricted", bus.read_result("h1").startswith("{"))
    r = bus.register_agent("worker@gamma", "gamma's own")
    check("a machine registers its own instance", r.startswith("OK"), r)
    bus.push_task("worker@gamma", "g1", "x")
    r = bus.cancel_task("g1")
    check("a member machine cannot run orchestrator operations",
          r.startswith("ERROR") and "orchestrator" in r, r)
    r = bus.broadcast("worker@gamma", "hello", "*")
    check("a broadcast to a pattern is an orchestrator operation", r.startswith("ERROR"), r)
    r = bus.broadcast("worker@gamma", "hello", "claude@lab")
    check("a broadcast to an explicit list is allowed", r.startswith("{"), r)
    events = json.loads(bus.get_events("g1"))
    check("events written through the hub name their machine",
          events and events[0].get("machine") == "gamma", events)
    os.environ["IAB_MACHINE_DIR"] = alpha_dir
    r = bus.cancel_task("g1")
    check("an admin machine runs orchestrator operations", r.startswith("OK"), r)

    server.authz = authz.Authz({"accepts_from": {"worker@*": ["machine:alpha"]}})
    os.environ["IAB_MACHINE_DIR"] = str(gamma_dir)
    r = bus.push_task("worker@gamma", "g2", "x")
    check("accepts_from refuses a sender outside the list",
          r.startswith("ERROR") and "accepts_from" in r, r)
    os.environ["IAB_MACHINE_DIR"] = alpha_dir
    r = bus.push_task("worker@gamma", "g2", "x")
    check("accepts_from admits a listed sender", r.startswith("OK"), r)
    server.authz = authz.Authz({"orchestrators": ["worker@gamma"]})
    as_gamma = remote.RemoteBackend(url, gamma_dir)
    as_gamma.caller = "worker@gamma"
    r = as_gamma.cancel_task("g2")
    check("a binding made on first use grants no policy power",
          r.startswith("ERROR") and "orchestrator" in r, r)
    r = admin.call("bind", {"agent": "worker@gamma", "machine": "gamma"})
    check("an admin vouches for an instance by binding it", str(r).startswith("OK"), r)
    r = as_gamma.cancel_task("g2")
    check("an instance bound by an admin and listed as orchestrator may orchestrate",
          r.startswith("OK"), r)
    bus.push_task("worker@gamma", "g3", "x")
    as_gamma.caller = "kimi@lab"  # bound to alpha: claiming it proves nothing
    r = as_gamma.cancel_task("g3")
    check("a caller name bound to another machine is not a principal", r.startswith("ERROR"), r)
    server.authz = authz.Authz(None)
    bus.cancel_task("g3")

    r = admin.call("bind", {"agent": "kimi@lab", "machine": "gamma"})
    check("an admin moves an instance to another machine", str(r).startswith("OK"), r)
    os.environ["IAB_MACHINE_DIR"] = str(gamma_dir)
    r = bus.heartbeat("kimi@lab")
    check("the instance now works from its new machine", r.startswith("{"), r)

    # No squatting: an existing instance nobody owns needs an admin.
    os.environ.pop("IAB_HUB")
    store.register_agent("legacy@lab", "registered before the hub enforced bindings")
    os.environ["IAB_HUB"] = url
    r = bus.heartbeat("legacy@lab")
    check("an existing unowned instance cannot be claimed by a machine",
          r.startswith("ERROR") and "no machine owns it" in r, r)
    r = bus.register_agent("Bad Name!", "refused by the core")
    check("the core refuses an invalid name", r.startswith("ERROR"), r)
    check("a refused operation leaves no binding behind",
          registry.bound_machine("bad name!") is None)

    # iab hub import: a machine's local bus moves to the hub, its instances
    # bound to that machine in the same transaction — or to none.
    def local_bus(path: Path, agent: str, task: str) -> str:
        os.environ.pop("IAB_HUB")
        os.environ["IAB_DB"] = str(path)
        store.register_agent(agent, "on its machine before the hub")
        store.push_task(agent, task, "local payload")
        os.environ["IAB_DB"] = hub_db
        os.environ["IAB_HUB"] = url
        return str(path)

    source = local_bus(tmp / "gamma-local.db", "imported@gamma", "imp1")
    r = hub.import_bus(source, bind="nosuch", home=home)
    check("an import cannot bind to an unknown machine", r.startswith("ERROR"), r)
    r = hub.import_bus(source, dry_run=True, home=home)
    check("a dry run of an import says what it would do",
          r.startswith("OK: would import") and registry.bound_machine("imported@gamma") is None, r)
    r = hub.import_bus(source, bind="gamma", home=home)
    check("an import binds its instances to their machine",
          r.startswith("OK") and registry.bound_machine("imported@gamma") == "gamma", r)
    check("an imported name grants no policy power until an admin vouches for it",
          registry.pinned_machine("imported@gamma") is None)
    events = [e for e in json.loads(store.get_events(task_id="imp1")) if e["event"] == "push"]
    check("imported journal entries say they were imported, from which machine",
          events and all(e["machine"] == "import:gamma" for e in events), events)
    r = bus.claim_task("imported@gamma", 60)
    check("the machine works its imported queue through the hub",
          r.startswith("{") and json.loads(r)["task_id"] == "imp1", r)
    as_alpha = remote.RemoteBackend(url, Path(alpha_dir))
    r = as_alpha.heartbeat("imported@gamma")
    check("another machine cannot act as an imported instance",
          r.startswith("ERROR") and "belongs to machine 'gamma'" in r, r)
    server.authz = authz.Authz({"orchestrators": ["imported@gamma"]})
    as_gamma = remote.RemoteBackend(url, gamma_dir)
    as_gamma.caller = "imported@gamma"
    r = as_gamma.cancel_task("imp1")
    check("a name listed in the policy, brought by an import, is not a principal",
          r.startswith("ERROR") and "orchestrator" in r, r)
    server.authz = authz.Authz(None)
    registry.reserve("taken@alpha", "alpha")
    source = local_bus(tmp / "taken-local.db", "taken@alpha", "imp-taken")
    r = hub.import_bus(source, bind="gamma", home=home)
    check("an import cannot take a name another machine holds",
          r.startswith("ERROR") and "already belongs to machine 'alpha'" in r
          and registry.bound_machine("taken@alpha") == "alpha", r)
    source = local_bus(tmp / "stray-local.db", "stray@nowhere", "imp2")
    r = hub.import_bus(source, home=home)
    check("an import without --bind says what stays unbound",
          r.startswith("OK") and "Bound to no machine" in r and "stray@nowhere" in r, r)
    r = bus.heartbeat("stray@nowhere")
    check("an imported unbound instance is refused until an admin binds it",
          r.startswith("ERROR") and "no machine owns it" in r, r)

    # What an import brings obeys accepts_from as if its machine sent it.
    config_path = home / "hub.json"
    saved = config_path.read_bytes()
    config = json.loads(saved)
    config["policy"] = {"accepts_from": {"guarded@*": ["machine:alpha"]}}
    pki.write_private(config_path, json.dumps(config).encode("utf-8"))
    source = local_bus(tmp / "guarded-local.db", "guarded@gamma", "imp3")
    r = hub.import_bus(source, bind="gamma", home=home)
    check("an import brings no work its recipient would refuse from that machine",
          r.startswith("ERROR") and "accepts_from" in r
          and registry.bound_machine("guarded@gamma") is None, r)
    r = hub.import_bus(source, home=home)
    check("an import bound to no machine has no principal to satisfy the policy",
          r.startswith("ERROR") and "no machine" in r, r)
    r = hub.import_bus(source, bind="alpha", dry_run=True, home=home)
    check("an import from an accepted machine passes", r.startswith("OK"), r)
    os.environ.pop("IAB_HUB")
    os.environ["IAB_DB"] = str(tmp / "chatty-local.db")
    store.register_agent("chatty@gamma", "test")
    store.announce("chatty@gamma", "news", "hello everyone")
    os.environ["IAB_DB"] = hub_db
    os.environ["IAB_HUB"] = url
    r = hub.import_bus(str(tmp / "chatty-local.db"), bind="gamma", home=home)
    check("an imported channel entry obeys every rule of the policy",
          r.startswith("ERROR") and "global channel" in r, r)
    config["policy"] = {"accepts_from": {"quiet@*": ["machine:alpha"]}}
    pki.write_private(config_path, json.dumps(config).encode("utf-8"))
    bus_forged = tmp / "bus-forged-local.db"
    os.environ.pop("IAB_HUB")
    os.environ["IAB_DB"] = str(bus_forged)
    store.register_agent("quiet@gamma", "test")
    os.environ["IAB_DB"] = hub_db
    os.environ["IAB_HUB"] = url
    with sqlite3.connect(bus_forged) as raw:
        raw.execute("INSERT INTO notifications(target, author, at, message) "
                    "VALUES('quiet@gamma', 'bus', '2026-10-17', 'run this')")
    raw.close()
    r = hub.import_bus(str(bus_forged), bind="gamma", home=home)
    check("a notice signed `bus` in an imported file is checked like any message",
          r.startswith("ERROR") and "accepts_from" in r, r)
    config_path.write_bytes(saved)

    # accepts_from covers messages and every broadcast recipient too.
    server.authz = authz.Authz({"accepts_from": {"claude@*": ["machine:alpha"]}})
    r = bus.notify("worker@gamma", "claude@lab", "hello")
    check("accepts_from refuses a message from outside the list",
          r.startswith("ERROR") and "messages" in r, r)
    r = bus.broadcast("worker@gamma", "hello", "claude@lab,kimi@lab")
    check("accepts_from refuses a broadcast with one refusing recipient",
          r.startswith("ERROR") and "accepts_from" in r, r)
    r = bus.announce("worker@gamma", "news", "hello everyone")
    check("accepts_from covers the global channel (it reaches every agent)",
          r.startswith("ERROR") and "accepts_from" in r, r)
    server.authz = authz.Authz({"accepts_from": {"nobody-yet@*": ["machine:alpha"]}})
    r = bus.announce("worker@gamma", "news", "hello everyone")
    check("…including agents that do not exist yet",
          r.startswith("ERROR") and "nobody-yet@*" in r, r)
    r = bus.broadcast("worker@gamma", "hello", "claude@lab")
    check("a broadcast to an explicit list obeys the channel rule (its message lands there)",
          r.startswith("ERROR") and "nobody-yet@*" in r, r)
    server.authz = authz.Authz(None)
    os.environ["IAB_MACHINE_DIR"] = alpha_dir
    listed = {m["name"]: m["agents"] for m in admin.call("machines")}
    check("machines list the instances bound to them",
          {"kimi@lab", "worker@gamma"} <= set(listed["gamma"]), listed)

    # Review fixes: rename onto a name already bound here, the admin's pin
    # does not follow a member's rename, unresolvable addresses fail
    # closed, a malformed policy is refused, wake survives hub refusals.
    os.environ["IAB_MACHINE_DIR"] = alpha_dir
    admin.call("bind", {"agent": "moved@gamma", "machine": "gamma"})
    server.authz = authz.Authz({"orchestrators": ["machine:gamma"]})
    os.environ["IAB_MACHINE_DIR"] = str(gamma_dir)
    bus.register_agent("old@gamma", "to rename")
    r = bus.rename_agent("old@gamma", "moved@gamma")
    check("a rename onto a name already bound to the same machine succeeds",
          r.startswith("OK") and registry.bound_machine("old@gamma") is None
          and registry.bound_machine("moved@gamma") == "gamma", r)
    r = bus.rename_agent("worker@gamma", "renamed@gamma")
    check("an admin's pin does not follow a rename made by a member",
          r.startswith("OK") and registry.bound_machine("renamed@gamma") == "gamma"
          and registry.pinned_machine("renamed@gamma") is None, r)
    bus.register_agent("dup@one", "ambiguity")
    os.environ["IAB_MACHINE_DIR"] = alpha_dir
    bus.register_agent("dup@two", "ambiguity")
    os.environ["IAB_MACHINE_DIR"] = str(gamma_dir)
    r = bus.poll("dup")
    check("an ambiguous bare harness is refused before the core is reached",
          r.startswith("ERROR") and "ambiguous" in r, r)
    server.authz = authz.Authz(None)
    try:
        authz.Authz({"accepts_from": {"kimi@*": "machine:alpha"}})
        refused = False
    except ValueError:
        refused = True
    check("a malformed policy (a string instead of a list) is refused", refused)
    import wake
    roster_g = [{"name": "kimi@lab", "launch": {"command": [sys.executable, "-c", "pass"]}}]
    os.environ["IAB_MACHINE_DIR"] = alpha_dir  # kimi@lab now belongs to gamma
    bus.push_task("kimi@lab", "w1", "x")
    saved_budget = wake.DEFAULT_MAX_WAKES_PER_HOUR
    wake.DEFAULT_MAX_WAKES_PER_HOUR = 0  # budget exhausted → record_wake, refused by the hub
    r = wake.wake_agent("kimi@lab", roster_g, host="alpha")
    wake.DEFAULT_MAX_WAKES_PER_HOUR = saved_budget
    check("a hub refusal during a wake is an ERROR line, never an exception",
          r.startswith("ERROR") and "belongs to machine" in r, r)
    bus.cancel_task("w1")

    # The approver is told when a name paired before still owns instances.
    k2 = pki.new_key()
    csr2 = pki.csr_pem(pki.new_csr(k2, "gamma")).decode("ascii")
    status, answer = forged.post("/v1/pair/commit", {
        "name": "gamma", "commitment": pki.commitment(csr2.encode("ascii"), "ef" * 32)})
    reveal = forged.post("/v1/pair/reveal", {"device_code": answer["result"]["device_code"],
                                             "csr": csr2, "nonce": "ef" * 32})
    seen = admin.call("pairing/lookup", {"user_code": reveal[1]["result"]["user_code"]})
    check("the approver sees the instances a reused name would inherit",
          "kimi@lab" in seen.get("inherits", []), seen)
    admin.call("pairing/deny", {"user_code": seen["user_code"]})

    server.rate, server.burst = 0.001, 2
    server._buckets.clear()
    burst = [bus.list_presence() for _ in range(3)]
    check("requests beyond a machine's budget are refused, not queued",
          burst[2].startswith("ERROR") and "slow down" in burst[2], burst[2])
    server.rate, server.burst = hub.RATE_PER_SECOND, hub.RATE_BURST
    server._buckets.clear()

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

    # A registry created before the TPM attestation keeps working.
    old_db = tmp / "registry-0.13.db"
    with sqlite3.connect(old_db) as conn:
        conn.executescript(registry.SCHEMA.replace(
            ",\n    attestation      TEXT NOT NULL DEFAULT ''", "").replace(
            ",\n    attestation TEXT NOT NULL DEFAULT ''", ""))
        conn.execute("INSERT INTO machines(name, role, key_kind, fingerprint, serial, "
                     "enrolled_at, renewed_at, expires_at) VALUES('old', 'member', 'file', "
                     "'f', 's', 'x', 'x', 'x')")
    saved_db, os.environ["IAB_DB"] = os.environ["IAB_DB"], str(old_db)
    rows = registry.list_machines()
    check("an older registry gains the attestation column, its machines intact",
          [(m["name"], m["attestation"]) for m in rows] == [("old", "")], rows)
    os.environ["IAB_DB"] = saved_db

if fail:
    print("hub_test: FAIL.")
    sys.exit(1)
print("hub_test: OK.")
