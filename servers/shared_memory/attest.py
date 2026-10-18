"""TPM attestation, hub side — proof that a machine key was born in a TPM
chip and can never leave it, by credential activation against the chip's
endorsement key (EK).

Owner's decision of 2026-10-17 (phase 9e): a TPM key is attested, never
taken on the machine's word — a machine that merely declared `key: tpm`
could keep its key in a file and still become admin.

The exchange (pairing.py drives it, the client side is tpm.py):
  1. the joining machine sends, next to its certificate request, the
     public area of its key and of its EK (TPM2B_PUBLIC), the EK
     certificate when the chip carries one, and the intermediate
     certificates it found;
  2. the hub checks the key's attributes — fixedTPM, fixedParent,
     sensitiveDataOrigin: generated inside the chip, impossible to export
     or duplicate — that it is the key of the request, and that the EK is
     genuine: certified by a manufacturer CA shipped with the hub (or added
     by the operator), or trusted by an admin through its fingerprint (a
     virtual TPM has no manufacturer certificate);
  3. the hub encrypts a random secret to the EK, bound to the key's name
     (TPM2_MakeCredential, computed here in software): only the chip that
     holds that EK, with that very key loaded, gives the secret back
     (TPM2_ActivateCredential) — a public area loaded alone cannot be
     activated, so a software key dressed up with the right attributes
     gets nothing;
  4. the machine returns the secret with its request.

What it proves: the key is non-exportable, in a chip whose EK the hub
trusts. What it does not prove: which person may pair — a human approval
(or an invitation) still decides that.

EK certificates are checked by signature only, never by date: the
manufacturer's signature states that the EK was born in its chip, a fact
an expiry date does not change (TPMs outlive their certificates).

Needs only `cryptography` (extra `[hub]`), like pki.py.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:  # installed package (`iab` maps to this directory)
    from . import pki
except ImportError:  # bare script usage
    import pki

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.x509.oid import NameOID

try:  # recent cryptography: CFB lives with the legacy modes (the TPM mandates it)
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # pragma: no cover — older cryptography
    from cryptography.hazmat.primitives.ciphers.modes import CFB

# TPM 2.0 algorithm and curve identifiers (TCG Algorithm Registry).
ALG_RSA = 0x0001
ALG_AES = 0x0006
ALG_SHA256 = 0x000B
ALG_SHA384 = 0x000C
ALG_NULL = 0x0010
ALG_ECC = 0x0023
ALG_CFB = 0x0043
ECC_NIST_P256 = 0x0003
ECC_NIST_P384 = 0x0004

# TPMA_OBJECT bits.
FIXED_TPM = 1 << 1
FIXED_PARENT = 1 << 4
SENSITIVE_DATA_ORIGIN = 1 << 5
USER_WITH_AUTH = 1 << 6
ADMIN_WITH_POLICY = 1 << 7
RESTRICTED = 1 << 16
DECRYPT = 1 << 17
SIGN = 1 << 18
# Born in the chip, bound to it and to its parent: never exported.
BOUND = FIXED_TPM | FIXED_PARENT | SENSITIVE_DATA_ORIGIN

_HASHES = {ALG_SHA256: ("sha256", hashes.SHA256), ALG_SHA384: ("sha384", hashes.SHA384)}
_CURVES = {ECC_NIST_P256: ec.SECP256R1, ECC_NIST_P384: ec.SECP384R1}

ROOTS = Path(__file__).with_name("tpm_roots.pem")
MAX_PUBLIC = 1024        # a TPM2B_PUBLIC is a few hundred bytes
MAX_CERTIFICATE = 8192
MAX_CHAIN = 4
SECRET_BYTES = 32        # the credential: one SHA-256 digest


class AttestationError(ValueError):
    pass


# -- TPM structures ----------------------------------------------------------

class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise AttestationError("truncated TPM structure")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u16(self) -> int:
        return int.from_bytes(self.take(2), "big")

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "big")

    def sized(self) -> bytes:
        return self.take(self.u16())

    def scheme(self) -> int:
        # TPMT_*_SCHEME / TPMT_KDF_SCHEME: an algorithm, then its hash
        # unless NULL (multi-part schemes such as ECDAA are refused).
        alg = self.u16()
        if alg != ALG_NULL:
            self.u16()
        return alg


def _sized(data: bytes) -> bytes:
    return len(data).to_bytes(2, "big") + data


@dataclass(frozen=True)
class Public:
    """A parsed TPMT_PUBLIC (the part of an object its name hashes)."""
    area: bytes
    type: int
    name_alg: int
    attributes: int
    auth_policy: bytes
    symmetric: tuple[int, int, int]  # (algorithm, key bits, mode); NULL → (NULL, 0, 0)
    key: object                       # the public key, as a cryptography object

    @property
    def name(self) -> bytes:
        """The object's TPM name: its name algorithm, then the digest of its
        public area — what a credential is bound to."""
        algorithm, _ = _HASHES[self.name_alg]
        return self.name_alg.to_bytes(2, "big") + hashlib.new(algorithm, self.area).digest()


def parse_public(blob: bytes) -> Public:
    """Parse a marshaled TPM2B_PUBLIC (RSA or ECC object)."""
    outer = _Reader(blob)
    area = outer.sized()
    if outer.pos != len(blob):
        raise AttestationError("trailing bytes after the TPM public area")
    r = _Reader(area)
    kind, name_alg, attributes = r.u16(), r.u16(), r.u32()
    if name_alg not in _HASHES:
        raise AttestationError(f"unsupported TPM name algorithm 0x{name_alg:04x}")
    auth_policy = r.sized()
    sym_alg = r.u16()
    symmetric = (sym_alg, r.u16(), r.u16()) if sym_alg != ALG_NULL else (ALG_NULL, 0, 0)
    if kind == ALG_RSA:
        r.scheme()
        bits, exponent = r.u16(), r.u32() or 65537
        modulus = r.sized()
        if len(modulus) * 8 != bits:
            raise AttestationError("RSA modulus does not match its declared size")
        try:
            key = rsa.RSAPublicNumbers(exponent, int.from_bytes(modulus, "big")).public_key()
        except ValueError as exc:
            raise AttestationError(f"invalid RSA key in the TPM public area: {exc}") from None
    elif kind == ALG_ECC:
        r.scheme()
        curve_id = r.u16()
        r.scheme()  # kdf
        x, y = r.sized(), r.sized()
        if curve_id not in _CURVES:
            raise AttestationError(f"unsupported TPM curve 0x{curve_id:04x}")
        try:
            key = ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"), int.from_bytes(y, "big"), _CURVES[curve_id]()
            ).public_key()
        except ValueError as exc:
            raise AttestationError(f"invalid ECC point in the TPM public area: {exc}") from None
    else:
        raise AttestationError(f"unsupported TPM object type 0x{kind:04x}")
    if r.pos != len(area):
        raise AttestationError("trailing bytes inside the TPM public area")
    return Public(area, kind, name_alg, attributes, auth_policy, symmetric, key)


# -- TPM2_MakeCredential, in software ----------------------------------------

def kdfa(name_alg: int, key: bytes, label: bytes, context_u: bytes, context_v: bytes,
         bits: int) -> bytes:
    """TPM 2.0 KDFa (SP 800-108 counter mode, HMAC)."""
    algorithm, _ = _HASHES[name_alg]
    out, counter = b"", 1
    while len(out) * 8 < bits:
        out += hmac.new(key, counter.to_bytes(4, "big") + label + b"\0" + context_u
                        + context_v + bits.to_bytes(4, "big"), algorithm).digest()
        counter += 1
    return out[:bits // 8]


def kdfe(name_alg: int, z: bytes, label: bytes, party_u: bytes, party_v: bytes,
         bits: int) -> bytes:
    """TPM 2.0 KDFe (SP 800-56A concatenation KDF), for ECC secrets."""
    algorithm, _ = _HASHES[name_alg]
    out, counter = b"", 1
    while len(out) * 8 < bits:
        out += hashlib.new(algorithm, counter.to_bytes(4, "big") + z + label + b"\0"
                           + party_u + party_v).digest()
        counter += 1
    return out[:bits // 8]


def _seed(ek: Public) -> tuple[bytes, bytes]:
    """A fresh seed and its encryption to the EK (TPM2B_ENCRYPTED_SECRET)."""
    algorithm, hash_class = _HASHES[ek.name_alg]
    size = hashlib.new(algorithm).digest_size
    if ek.type == ALG_RSA:
        seed = os.urandom(size)
        encrypted = ek.key.encrypt(seed, padding.OAEP(mgf=padding.MGF1(hash_class()),
                                                      algorithm=hash_class(),
                                                      label=b"IDENTITY\0"))
        return seed, _sized(encrypted)
    curve = ek.key.curve
    width = (curve.key_size + 7) // 8
    ephemeral = ec.generate_private_key(type(curve)())
    z = ephemeral.exchange(ec.ECDH(), ek.key)
    point = ephemeral.public_key().public_numbers()
    qx, qy = point.x.to_bytes(width, "big"), point.y.to_bytes(width, "big")
    ek_x = ek.key.public_numbers().x.to_bytes(width, "big")
    seed = kdfe(ek.name_alg, z, b"IDENTITY", qx, ek_x, size * 8)
    return seed, _sized(_sized(qx) + _sized(qy))


def make_credential(ek: Public, credential: bytes, name: bytes) -> tuple[bytes, bytes]:
    """TPM2_MakeCredential: `credential` sealed for the chip holding `ek`,
    releasable only for the object called `name`. Returns the marshaled
    TPM2B_ID_OBJECT and TPM2B_ENCRYPTED_SECRET."""
    algorithm, _ = _HASHES[ek.name_alg]
    size = hashlib.new(algorithm).digest_size
    if len(credential) > size:
        raise AttestationError("credential longer than the EK's name digest")
    seed, secret = _seed(ek)
    sym_key = kdfa(ek.name_alg, seed, b"STORAGE", name, b"", ek.symmetric[1])
    encryptor = Cipher(algorithms.AES(sym_key), CFB(b"\0" * 16)).encryptor()
    identity = encryptor.update(_sized(credential)) + encryptor.finalize()
    hmac_key = kdfa(ek.name_alg, seed, b"INTEGRITY", b"", b"", size * 8)
    integrity = hmac.new(hmac_key, identity + name, algorithm).digest()
    return _sized(_sized(integrity) + identity), secret


# -- what the hub accepts ----------------------------------------------------

def _check_key(key: Public) -> None:
    if key.attributes & BOUND != BOUND:
        raise AttestationError("the key was not generated inside the TPM or can leave it "
                               "(fixedTPM, fixedParent and sensitiveDataOrigin required)")
    if not key.attributes & SIGN or key.attributes & (RESTRICTED | DECRYPT):
        raise AttestationError("the key must be an unrestricted signing key")
    # Authorized by its secret value only: a public area loaded alone in a
    # chip has none, so it cannot be activated. Chips also refuse a policy
    # on such an object (checked on hardware, tpm_test.py); the hub does
    # not rely on it.
    if key.auth_policy or key.attributes & ADMIN_WITH_POLICY \
            or not key.attributes & USER_WITH_AUTH:
        raise AttestationError("the key must be authorized by its own secret, not by a "
                               "policy (empty authPolicy, userWithAuth, no adminWithPolicy)")
    if key.type != ALG_ECC or not isinstance(key.key.curve, ec.SECP256R1) \
            or key.name_alg != ALG_SHA256:
        raise AttestationError("the key must be an ECC P-256 key named with SHA-256")


def _check_ek(ek: Public) -> None:
    if ek.attributes & BOUND != BOUND or ek.attributes & (RESTRICTED | DECRYPT) \
            != RESTRICTED | DECRYPT or ek.attributes & SIGN:
        raise AttestationError("the endorsement key is not a restricted decryption key "
                               "bound to its chip")
    alg, bits, mode = ek.symmetric
    if alg != ALG_AES or mode != ALG_CFB or bits not in (128, 256):
        raise AttestationError("the endorsement key does not use AES-CFB")
    if ek.type == ALG_RSA and ek.key.key_size < 2048:
        raise AttestationError("the endorsement key is an RSA key under 2048 bits")


def _cn(name: x509.Name) -> str:
    attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attrs[0].value) if attrs else name.rfc4514_string()


def issuer_name(cert: x509.Certificate) -> str:
    return _cn(cert.issuer)


def _signed(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    # Not verify_directly_issued_by: it compares the names' DER bytes, and
    # manufacturers encode the same name differently in the EK certificate
    # and in their CA's (PrintableString / UTF8String). The callers match
    # names by value, as OpenSSL does; this checks the signature.
    key = issuer.public_key()
    try:
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(cert.signature, cert.tbs_certificate_bytes,
                       cert.signature_algorithm_parameters, cert.signature_hash_algorithm)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(cert.signature, cert.tbs_certificate_bytes,
                       cert.signature_algorithm_parameters)
        else:
            return False
    except (InvalidSignature, ValueError, TypeError, UnsupportedAlgorithm):
        return False
    return True


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        return False


def load_certificates(data: bytes) -> list[x509.Certificate]:
    """Every certificate of a PEM bundle, or the one of a DER file."""
    if b"-----BEGIN CERTIFICATE-----" in data:
        return x509.load_pem_x509_certificates(data)
    return [x509.load_der_x509_certificate(data)]


class Trust:
    """The CAs a hub accepts for EK certificates: the manufacturers' roots
    shipped with the hub (tpm_roots.pem), plus whatever the operator drops
    in <hub home>/tpm-ca (PEM or DER) — a missing intermediate, a
    manufacturer the bundle lacks, the CA of a virtual TPM's host."""

    def __init__(self, extra: Path | None = None) -> None:
        self.anchors: list[x509.Certificate] = []
        self.anchors += load_certificates(ROOTS.read_bytes())
        if extra is not None and extra.is_dir():
            for path in sorted(extra.iterdir()):
                if path.suffix.lower() in (".pem", ".crt", ".cer", ".der"):
                    try:
                        self.anchors += load_certificates(path.read_bytes())
                    except ValueError as exc:
                        raise AttestationError(f"unreadable CA file {path}: {exc}") from None

    def certified_by(self, cert: x509.Certificate, chain: list[x509.Certificate]) -> str:
        """The name of the CA that issued `cert`, once its chain reaches a
        trusted CA; AttestationError otherwise."""
        current, pool = cert, list(chain)
        for _ in range(MAX_CHAIN + 2):
            for anchor in self.anchors:
                if anchor.subject == current.issuer and _signed(current, anchor):
                    return _cn(cert.issuer)
            for candidate in pool:
                if candidate.subject == current.issuer and _is_ca(candidate) \
                        and _signed(current, candidate):
                    current = candidate
                    pool.remove(candidate)
                    break
            else:
                raise AttestationError(f"the EK certificate does not chain to a TPM "
                                       f"manufacturer CA this hub trusts (issuer: "
                                       f"'{_cn(current.issuer)}')")
        raise AttestationError("the EK certificate chain is too long")


@dataclass(frozen=True)
class Checked:
    ek: Public
    name: bytes             # the machine key's TPM name
    ek_fingerprint: str
    description: str        # how the EK was trusted, for the registry and the approver


def _decode(value, limit: int, what: str) -> bytes:
    if not isinstance(value, str):
        raise AttestationError(f"missing {what}")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise AttestationError(f"{what} is not base64") from None
    if not data or len(data) > limit:
        raise AttestationError(f"{what} has an unexpected size")
    return data


def check(material, csr_key, trust: Trust,
          pinned: Callable[[str], dict | None]) -> Checked | str:
    """Check what a joining machine sends about its TPM (see the module
    docstring, step 2). `pinned(fingerprint)` answers whether an admin
    trusts that EK. Returns what the challenge needs, or an ERROR line."""
    try:
        if not isinstance(material, dict):
            raise AttestationError("a TPM key needs its attestation material")
        key = parse_public(_decode(material.get("public"), MAX_PUBLIC, "the key's public area"))
        _check_key(key)
        if not pki.same_key(key.key, csr_key):
            raise AttestationError("the TPM key is not the key of the certificate request")
        ek = parse_public(_decode(material.get("ek_public"), MAX_PUBLIC,
                                  "the endorsement key's public area"))
        _check_ek(ek)
        fingerprint = pki.spki_sha256(ek.key)
        pin = pinned(fingerprint)
        if pin is not None:
            return Checked(ek, key.name, fingerprint,
                           f"EK sha256:{fingerprint}, trusted by {pin['added_by']}")
        hint = (f" — an admin may trust it once, after reading its fingerprint on the "
                f"machine itself: iab trust-ek {fingerprint}")
        if material.get("ek_certificate") is None:
            raise AttestationError("no manufacturer certificate for this TPM's endorsement "
                                   "key (a virtual TPM?)" + hint)
        try:
            cert = x509.load_der_x509_certificate(
                _decode(material["ek_certificate"], MAX_CERTIFICATE, "the EK certificate"))
            raw_chain = material.get("chain") or []
            if not isinstance(raw_chain, list) or len(raw_chain) > MAX_CHAIN:
                raise AttestationError("unexpected EK certificate chain")
            chain = [x509.load_der_x509_certificate(
                _decode(c, MAX_CERTIFICATE, "an intermediate certificate")) for c in raw_chain]
        except AttestationError:
            raise
        except ValueError as exc:
            raise AttestationError(f"unreadable EK certificate ({exc})" + hint) from None
        if not pki.same_key(cert.public_key(), ek.key):
            raise AttestationError("the EK certificate is not for this endorsement key")
        try:
            issuer = trust.certified_by(cert, chain)
        except AttestationError as exc:
            raise AttestationError(f"{exc}{hint}") from None
        return Checked(ek, key.name, fingerprint,
                       f"EK sha256:{fingerprint}, certified by {issuer}")
    except AttestationError as exc:
        return f"ERROR: TPM attestation refused: {exc}."


def normalize_fingerprint(text: str) -> str | None:
    """An EK fingerprint as typed by a person (colons, spaces, any case)."""
    value = "".join(c for c in str(text).lower() if c not in ": ")
    return value if len(value) == 64 and all(c in "0123456789abcdef" for c in value) else None


def encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
