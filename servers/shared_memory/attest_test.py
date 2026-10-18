"""Tests of the hub side of TPM attestation (attest.py) — needs the
`[hub]` extra (cryptography), no TPM: a software stand-in (softtpm.py)
plays the chip. Compatibility with a real chip: tpm_test.py.

    python3 attest_test.py
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

import attest
import pki
import softtpm
from tpmtls import TpmError

fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global fail
    print(("ok — " if ok else "FAIL: ") + label + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        fail = 1


def refused(fn) -> bool:
    try:
        fn()
    except (attest.AttestationError, TpmError, ValueError):
        return True
    return False


# -- TPM structures -----------------------------------------------------------
chip = softtpm.SoftChip()
key = chip.new_key()
parsed = attest.parse_public(key.public_area)
check("a TPM public area is parsed back to its key",
      pki.same_key(parsed.key, key.public_key) and parsed.type == attest.ALG_ECC)
check("its name is the name algorithm, then the digest of the area",
      parsed.name[:2] == b"\x00\x0b" and len(parsed.name) == 34)
check("trailing bytes after a public area are refused",
      refused(lambda: attest.parse_public(key.public_area + b"\0")))
check("a truncated public area is refused",
      refused(lambda: attest.parse_public(key.public_area[:-3])))
bad_point = bytearray(key.public_area)
bad_point[-1] ^= 1
check("a point off the curve is refused", refused(lambda: attest.parse_public(bytes(bad_point))))
ek = attest.parse_public(chip.ek_area())
check("an RSA endorsement key is parsed with its symmetric scheme",
      ek.type == attest.ALG_RSA and ek.symmetric == (attest.ALG_AES, 128, attest.ALG_CFB))

# -- TPM2_MakeCredential ------------------------------------------------------
for kind in ("rsa", "ecc"):
    soft = softtpm.SoftChip(ek=kind)
    k = soft.new_key()
    soft_ek = attest.parse_public(soft.ek_area())
    secret = b"s" * attest.SECRET_BYTES
    blob, sealed = attest.make_credential(soft_ek, secret, attest.parse_public(k.public_area).name)
    challenge = {"credential_blob": attest.encode(blob), "encrypted_secret": attest.encode(sealed)}
    check(f"a credential sealed to a {kind.upper()} EK opens for the named key",
          soft.activate(k, None, challenge) == secret)
    other = soft.new_key()
    check(f"it does not open for another key ({kind.upper()} EK)",
          refused(lambda: soft.activate(other, None, challenge)))
check("a credential longer than the name digest is refused",
      refused(lambda: attest.make_credential(ek, b"x" * 33, parsed.name)))

# -- what the hub accepts -------------------------------------------------------
pins: dict[str, dict] = {}
trust = attest.Trust()
check("the shipped manufacturer roots load", len(trust.anchors) >= 30, len(trust.anchors))
check("they include STMicroelectronics' EK roots",
      {"STM TPM EK Root CA", "STM TPM ECC Root CA 01"}
      <= {attest._cn(a.subject) for a in trust.anchors})


def material(c: softtpm.SoftChip, k) -> dict:
    return {"public": attest.encode(k.public_area), **c.endorsement().material}


out = attest.check(material(chip, key), key.public_key, trust, pins.get)
check("an EK without certificate nor admin trust is refused",
      isinstance(out, str) and out.startswith("ERROR"), out)
fingerprint = pki.spki_sha256(chip.ek_key.public_key())
check("the refusal names the fingerprint to trust and the command",
      isinstance(out, str) and f"iab trust-ek {fingerprint}" in out, out)
pins[fingerprint] = {"added_by": "alpha"}
out = attest.check(material(chip, key), key.public_key, trust, pins.get)
check("an EK trusted by an admin is accepted",
      not isinstance(out, str) and "trusted by alpha" in out.description, out)
check("the check returns the key's name for the credential",
      not isinstance(out, str) and out.name == parsed.name)
out = attest.check(material(chip, key), pki.new_key().public_key(), trust, pins.get)
check("a TPM key other than the request's is refused",
      isinstance(out, str) and "not the key of the certificate request" in out, out)
for label, attributes in (
        ("a key that may leave the chip (no fixedTPM) is refused",
         softtpm.KEY_ATTRIBUTES & ~attest.FIXED_TPM),
        ("a key not generated in the chip (no sensitiveDataOrigin) is refused",
         softtpm.KEY_ATTRIBUTES & ~attest.SENSITIVE_DATA_ORIGIN),
        ("a duplicable key (no fixedParent) is refused",
         softtpm.KEY_ATTRIBUTES & ~attest.FIXED_PARENT),
        ("a restricted key is refused", softtpm.KEY_ATTRIBUTES | attest.RESTRICTED),
        ("a key authorized by a policy for its admin role is refused",
         softtpm.KEY_ATTRIBUTES | attest.ADMIN_WITH_POLICY),
        ("a key its secret cannot authorize (no userWithAuth) is refused",
         softtpm.KEY_ATTRIBUTES & ~attest.USER_WITH_AUTH)):
    odd = softtpm.SoftChip(key_attributes=attributes, ek_key=chip.ek_key)
    k = odd.new_key()
    out = attest.check(material(odd, k), k.public_key, trust, pins.get)
    check(label, isinstance(out, str) and out.startswith("ERROR"), out)
# A public area loaded alone in a chip has no secret: a policy is the only
# way it could be authorized — refused, whatever the chip would do.
odd = softtpm.SoftChip(ek_key=chip.ek_key, key_policy=b"\x01" * 32)
k = odd.new_key()
out = attest.check(material(odd, k), k.public_key, trust, pins.get)
check("a key carrying an authorization policy is refused",
      isinstance(out, str) and "authPolicy" in out, out)
weak = bytearray(chip.ek_area())
weak[-262:-258] = (2).to_bytes(4, "big")  # an even RSA exponent, before the modulus
odd_area = bytes(weak)
check("an invalid RSA key in a public area is a refusal, not a crash",
      refused(lambda: attest.parse_public(odd_area)))
odd = softtpm.SoftChip(ek_attributes=softtpm.EK_ATTRIBUTES & ~attest.RESTRICTED,
                       ek_key=chip.ek_key)
k = odd.new_key()
out = attest.check(material(odd, k), k.public_key, trust, pins.get)
check("an EK that is not a restricted decryption key is refused",
      isinstance(out, str) and "restricted decryption" in out, out)
broken = material(chip, key)
broken["public"] = "not base64!"
out = attest.check(broken, key.public_key, trust, pins.get)
check("malformed material is refused, not raised", isinstance(out, str), out)
check("missing material is refused",
      str(attest.check(None, key.public_key, trust, pins.get)).startswith("ERROR"))

# -- manufacturer certificates --------------------------------------------------
now = dt.datetime.now(dt.timezone.utc)


def name(cn: str, printable: bool = False) -> x509.Name:
    from cryptography.x509.name import _ASN1Type
    kind = _ASN1Type.PrintableString if printable else _ASN1Type.UTF8String
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test TPM Maker", kind),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn, kind)])


def issue(subject, issuer, public_key, signer, ca: bool, expired: bool = False):
    start = now - dt.timedelta(days=400 if expired else 1)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
               .public_key(public_key).serial_number(x509.random_serial_number())
               .not_valid_before(start)
               .not_valid_after(now + dt.timedelta(days=-10 if expired else 3650)))
    if ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
    return builder.sign(signer, hashes.SHA256())


root_key = ec.generate_private_key(ec.SECP384R1())
root = issue(name("Maker Root"), name("Maker Root"), root_key.public_key(), root_key, True)
int_key = rsa.generate_private_key(65537, 2048)
inter = issue(name("Maker Intermediate"), root.subject, int_key.public_key(), root_key, True)
maker = softtpm.SoftChip()
# Manufacturers encode the same name differently in the EK certificate and
# in their CA's (PrintableString / UTF8String): matched by value.
ek_cert = issue(x509.Name([]), name("Maker Intermediate", True),
                maker.ek_key.public_key(), int_key, False, expired=True)
der = lambda c: c.public_bytes(serialization.Encoding.DER)  # noqa: E731
with tempfile.TemporaryDirectory() as tmp:
    extra = Path(tmp)
    (extra / "maker-root.pem").write_bytes(root.public_bytes(serialization.Encoding.PEM))
    (extra / "notes.txt").write_text("ignored")
    local = attest.Trust(extra)
    certified = softtpm.SoftChip(ek_key=maker.ek_key, ek_certificate=der(ek_cert),
                                 chain=(der(inter),))
    k = certified.new_key()
    out = attest.check(material(certified, k), k.public_key, local, {}.get)
    check("an EK certified through an intermediate by a trusted root is accepted, "
          "whatever the name encoding and the certificate's dates",
          not isinstance(out, str) and "certified by Maker Intermediate" in out.description, out)
    out = attest.check(material(certified, k), k.public_key, trust, {}.get)
    check("the same EK is refused by a hub that does not trust that root, which is named",
          isinstance(out, str) and "'Maker Root'" in out and "trust-ek" in out, out)
    lone = softtpm.SoftChip(ek_key=maker.ek_key, ek_certificate=der(ek_cert))
    k = lone.new_key()
    out = attest.check(material(lone, k), k.public_key, local, {}.get)
    check("without its intermediate the chain does not reach the root",
          isinstance(out, str) and out.startswith("ERROR"), out)
    rogue_key = rsa.generate_private_key(65537, 2048)
    rogue = issue(name("Maker Intermediate"), root.subject, rogue_key.public_key(), rogue_key,
                  True)
    forged = softtpm.SoftChip(ek_key=maker.ek_key, ek_certificate=der(ek_cert),
                              chain=(der(rogue),))
    k = forged.new_key()
    out = attest.check(material(forged, k), k.public_key, local, {}.get)
    check("an intermediate of the right name but not signed by the root is refused",
          isinstance(out, str) and out.startswith("ERROR"), out)
    leaf_ca = issue(name("Maker Intermediate"), root.subject, int_key.public_key(), root_key,
                    False)
    not_ca = softtpm.SoftChip(ek_key=maker.ek_key, ek_certificate=der(ek_cert),
                              chain=(der(leaf_ca),))
    k = not_ca.new_key()
    out = attest.check(material(not_ca, k), k.public_key, local, {}.get)
    check("an intermediate that is not a CA is refused",
          isinstance(out, str) and out.startswith("ERROR"), out)
    stolen = softtpm.SoftChip(ek_certificate=der(ek_cert), chain=(der(inter),))
    k = stolen.new_key()
    out = attest.check(material(stolen, k), k.public_key, local, {}.get)
    check("a genuine EK certificate presented with another EK is refused",
          isinstance(out, str) and "not for this endorsement key" in out, out)
    (extra / "broken.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
    check("an unreadable CA file in the hub's folder is said loudly",
          refused(lambda: attest.Trust(extra)))

# -- fingerprints and requests signed elsewhere ------------------------------------
fp = "AB:" * 31 + "AB"
check("a fingerprint typed with colons and capitals is accepted",
      attest.normalize_fingerprint(fp) == "ab" * 32)
check("a short fingerprint is refused", attest.normalize_fingerprint("ab" * 31) is None)
held = pki.new_key()
signed = pki.signed_csr(held.public_key(), "kappa",
                        lambda data: held.sign(data, ec.ECDSA(hashes.SHA256())))
check("a request signed by an external signer is a valid request for that key",
      pki.same_key(pki.load_csr(pki.csr_pem(signed)).public_key(), held.public_key())
      and signed.subject == pki.new_csr(held, "kappa").subject)
other = pki.new_key()
check("a request whose external signature is wrong is refused",
      refused(lambda: pki.signed_csr(held.public_key(), "kappa",
                                     lambda data: other.sign(data, ec.ECDSA(hashes.SHA256())))))

print("attest_test: " + ("FAIL." if fail else "OK."))
sys.exit(fail)
