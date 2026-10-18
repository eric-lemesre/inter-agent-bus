"""Tests against the real TPM chip of this machine — the compatibility
that softtpm.py cannot prove: the chip opens the hub's credential (RSA
and ECC endorsement keys), signs the certificate request, and its key
carries the mutual TLS of every bus call, renewal included.

Skipped (exit 0) on a machine without a usable TPM. Needs the `[hub]`
extra, tpm2-pytss, tpm2-openssl and access to /dev/tpmrm0 (`tss` group;
`sg tss -c "…"` in a session opened before joining it). The EK
certificate's intermediates are fetched from the manufacturer (network).

    python3 tpm_test.py
"""
from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path

fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global fail
    print(("ok — " if ok else "FAIL: ") + label + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        fail = 1


import tpmtls  # noqa: E402

try:
    import tpm
    chip = tpm.Chip()
except (ImportError, tpmtls.TpmError) as exc:
    print(f"tpm_test: skipped — {exc}")
    sys.exit(0)

import attest  # noqa: E402
import pki  # noqa: E402

# -- the chip against the hub's side, without a hub ---------------------------------
key = chip.new_key()
csr = pki.load_csr(chip.csr(key, "chip-test").encode("ascii"))
check("the chip signs a valid certificate request for its own key",
      pki.same_key(csr.public_key(), key.public_key))
check("the key file is the chip's wrapping, not the key", key.pem.startswith(tpmtls.KEY_HEADER))
endorsement = chip.endorsement()
print(f"   EK {endorsement.ek_type} sha256 {endorsement.fingerprint} — "
      f"{endorsement.issuer or 'no manufacturer certificate'}; "
      f"{len(endorsement.material.get('chain', []))} intermediate(s) fetched")
material = {"public": attest.encode(key.public_area), **endorsement.material}
pinned = {}
checked = attest.check(material, csr.public_key(), attest.Trust(), pinned.get)
certified = not isinstance(checked, str)
if not certified:
    print(f"   not certified here ({checked}) — trusting the EK by fingerprint for the rest")
    pinned[endorsement.fingerprint] = {"added_by": "tpm_test"}
    checked = attest.check(material, csr.public_key(), attest.Trust(), pinned.get)
check("the hub accepts the chip's key and endorsement", not isinstance(checked, str), checked)
secret = secrets.token_bytes(attest.SECRET_BYTES)
blob, sealed = attest.make_credential(checked.ek, secret, checked.name)
opened = chip.activate(key, endorsement, {"credential_blob": attest.encode(blob),
                                          "encrypted_secret": attest.encode(sealed)})
check(f"the chip opens a credential sealed by the hub to its {endorsement.ek_type}",
      opened == secret)
from tpm2_pytss.utils import NVReadEK, create_ek_template  # noqa: E402

for ek_type in tpm.EK_TYPES:
    if ek_type == endorsement.ek_type:
        continue
    _, template = create_ek_template(ek_type, NVReadEK(chip.ectx))
    handle, public, _, _, _ = chip.ectx.create_primary(None, template,
                                                       chip._t.ESYS_TR.ENDORSEMENT)
    chip.ectx.flush_context(handle)
    other = tpm.Endorsement(ek_type, {}, "", None, template)
    blob, sealed = attest.make_credential(attest.parse_public(public.marshal()), secret,
                                          checked.name)
    check(f"the chip opens a credential sealed to its {ek_type} too",
          chip.activate(key, other, {"credential_blob": attest.encode(blob),
                                     "encrypted_secret": attest.encode(sealed)}) == secret)
wrong = bytearray(checked.name)
wrong[-1] ^= 1
blob, sealed = attest.make_credential(checked.ek, secret, bytes(wrong))
try:
    chip.activate(key, endorsement, {"credential_blob": attest.encode(blob),
                                     "encrypted_secret": attest.encode(sealed)})
    refused = False
except tpmtls.TpmError:
    refused = True
check("the chip refuses a credential made for another key", refused)

# The attack the hub's key checks guard against, tried on the chip itself:
# a software key's public area, loaded alone, authorized by a policy
# instead of a secret it does not have (the hub refuses such an area
# before any challenge — this checks the chip would not help either).
t = chip._t
trial = chip.ectx.start_auth_session(t.ESYS_TR.NONE, t.ESYS_TR.NONE, t.TPM2_SE.TRIAL,
                                     t.TPMT_SYM_DEF(algorithm=t.TPM2_ALG.NULL),
                                     t.TPM2_ALG.SHA256)
chip.ectx.policy_command_code(trial, t.TPM2_CC.ActivateCredential)
policy = bytes(chip.ectx.policy_get_digest(trial))
chip.ectx.flush_context(trial)
import softtpm  # noqa: E402

software = pki.new_key()
area = softtpm.ecc_area(software.public_key(),
                        softtpm.KEY_ATTRIBUTES | attest.ADMIN_WITH_POLICY, policy=policy)
check("the hub refuses a key area authorized by a policy",
      isinstance(attest.check({"public": attest.encode(area), **endorsement.material},
                              software.public_key(), attest.Trust(),
                              lambda fp: {"added_by": "tpm_test"}), str))
public, _ = t.TPM2B_PUBLIC.unmarshal(area)
loaded = chip.ectx.load_external(public, None, t.ESYS_TR.RH_NULL)
blob, sealed = attest.make_credential(checked.ek, secret, attest.parse_public(area).name)
ek, _, _, _, _ = chip.ectx.create_primary(None, endorsement.template, t.ESYS_TR.ENDORSEMENT)
sessions = []
for _ in range(2):
    sessions.append(chip.ectx.start_auth_session(
        t.ESYS_TR.NONE, t.ESYS_TR.NONE, t.TPM2_SE.POLICY,
        t.TPMT_SYM_DEF(algorithm=t.TPM2_ALG.NULL), t.TPM2_ALG.SHA256))
chip.ectx.policy_command_code(sessions[0], t.TPM2_CC.ActivateCredential)
chip.ectx.policy_secret(t.ESYS_TR.ENDORSEMENT, sessions[1], b"", b"", b"", 0)
try:
    chip.ectx.activate_credential(loaded, ek, t.TPM2B_ID_OBJECT.unmarshal(blob)[0],
                                  t.TPM2B_ENCRYPTED_SECRET.unmarshal(sealed)[0], *sessions)
    opened_by_policy = True
except t.TSS2_Exception:
    opened_by_policy = False
check("the chip itself refuses a public area loaded alone, even through a policy",
      not opened_by_policy)
for handle in (*sessions, ek, loaded):
    chip.ectx.flush_context(handle)
chip.close()

# -- end to end: pairing as admin, then every call signed by the chip ----------------
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    for var in ("IAB_DB", "ORCHESTRATOR_DB", "IAB_HUB", "HTTPS_PROXY", "https_proxy"):
        os.environ.pop(var, None)
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-hub")
    import socket

    import access
    import admin
    import hub
    import join
    import registry

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    home = tmp / "hub"
    port, pair_port = free_port(), free_port()
    out = hub.init(["127.0.0.1"], "hubhost", home, port=port, pair_port=pair_port)
    check("a hub is created on the loopback interface", out.startswith("OK"), out)
    server, pair_server = hub.serve_in_thread(home, ("127.0.0.1", port), ("127.0.0.1", pair_port))
    os.environ["IAB_HUB"] = f"https://127.0.0.1:{port}"
    for fingerprint in pinned:
        registry.trust_ek(fingerprint, "tpm_test", "hubhost")
    token = admin.call("invite", {"name": "chip", "role": "admin"})["token"]
    lines: list[str] = []
    chip = tpm.Chip()
    rc = join.join(f"127.0.0.1:{pair_port}", "chip", token, tmp / "machine-chip",
                   say=lines.append, chip=chip)
    chip.close()
    check("this machine pairs as admin with its TPM key", rc == 0, lines[-3:])
    row = registry.get("chip") or {}
    check("the hub records the key as tpm, with how it was proven",
          row.get("key_kind") == "tpm" and endorsement.fingerprint in row.get("attestation", ""),
          row)
    if certified:
        check("…certified by the chip's manufacturer", "certified by" in row.get("attestation", ""),
              row.get("attestation"))
    stored = (tmp / "machine-chip" / "machine.key").read_bytes()
    check("what the machine keeps is the chip's wrapping", stored.startswith(tpmtls.KEY_HEADER))
    os.environ["IAB_MACHINE_DIR"] = str(tmp / "machine-chip")
    bus = access.bus
    r = bus.register_agent("worker@chip", "TPM-signed calls")
    check("a bus call is carried by mutual TLS with the TPM key", r.startswith("OK"), r)
    check("…and the next ones too", bus.list_presence().startswith("["))
    machines = admin.call("machines")
    check("the TPM machine uses admin actions", isinstance(machines, list), machines)
    renewed = access.backend().renew(force=True)
    check("the certificate is renewed for the same chip key", renewed.startswith("OK"), renewed)
    check("calls go on after the renewal", bus.list_presence().startswith("["))
    server.shutdown()
    if pair_server is not None:
        pair_server.shutdown()

print("tpm_test: " + ("FAIL." if fail else "OK."))
sys.exit(fail)
