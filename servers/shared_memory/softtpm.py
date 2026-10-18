"""Test helper: a software stand-in for a TPM chip, with the interface of
tpm.Chip. It builds the public areas a chip would and answers the hub's
challenge by inverting TPM2_MakeCredential with keys it holds in memory.

It proves nothing — which is what the tests using it check: the hub must
refuse it unless its endorsement key is certified by a CA the hub trusts
or trusted by an admin. Compatibility with a real chip is tpm_test.py's
job.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

import attest
import pki
from attest import CFB, _Reader, _sized
from tpmtls import TpmError

NO_DA = 1 << 10
KEY_ATTRIBUTES = attest.BOUND | attest.USER_WITH_AUTH | NO_DA | attest.SIGN
EK_ATTRIBUTES = attest.BOUND | attest.ADMIN_WITH_POLICY | attest.RESTRICTED | attest.DECRYPT
# TPM2_PolicySecret(TPM_RH_ENDORSEMENT): the standard EK policy (template L-1).
EK_POLICY = bytes.fromhex("837197674484b3f81a90cc8d46a5d724fd52d76e06520b64f2a1da1b331469aa")


def _u16(value: int) -> bytes:
    return value.to_bytes(2, "big")


def ecc_area(public_key, attributes: int, symmetric: bool = False,
             policy: bytes = b"") -> bytes:
    numbers = public_key.public_numbers()
    width = (public_key.curve.key_size + 7) // 8
    curve = {256: attest.ECC_NIST_P256, 384: attest.ECC_NIST_P384}[public_key.curve.key_size]
    sym = (_u16(attest.ALG_AES) + _u16(128) + _u16(attest.ALG_CFB)) if symmetric \
        else _u16(attest.ALG_NULL)
    area = (_u16(attest.ALG_ECC) + _u16(attest.ALG_SHA256) + attributes.to_bytes(4, "big")
            + _sized(policy) + sym + _u16(attest.ALG_NULL) + _u16(curve)
            + _u16(attest.ALG_NULL) + _sized(numbers.x.to_bytes(width, "big"))
            + _sized(numbers.y.to_bytes(width, "big")))
    return _sized(area)


def rsa_ek_area(public_key, attributes: int = EK_ATTRIBUTES) -> bytes:
    n = public_key.public_numbers().n
    area = (_u16(attest.ALG_RSA) + _u16(attest.ALG_SHA256) + attributes.to_bytes(4, "big")
            + _sized(EK_POLICY) + _u16(attest.ALG_AES) + _u16(128) + _u16(attest.ALG_CFB)
            + _u16(attest.ALG_NULL) + _u16(public_key.key_size) + (0).to_bytes(4, "big")
            + _sized(n.to_bytes(public_key.key_size // 8, "big")))
    return _sized(area)


@dataclass
class SoftKey:
    pem: bytes
    public_area: bytes
    public_key: object
    handle: object  # the private key itself: this chip is make-believe


@dataclass
class SoftEndorsement:
    ek_type: str
    material: dict
    fingerprint: str
    issuer: str | None
    template: object = None


class SoftChip:
    def __init__(self, ek: str = "rsa", key_attributes: int = KEY_ATTRIBUTES,
                 ek_attributes: int = EK_ATTRIBUTES, ek_certificate: bytes | None = None,
                 chain: tuple = (), ek_key=None, key_policy: bytes = b"") -> None:
        if ek_key is not None:
            self.ek_key = ek_key
        elif ek == "rsa":
            self.ek_key = rsa.generate_private_key(65537, 2048)
        else:
            self.ek_key = ec.generate_private_key(ec.SECP256R1())
        self.key_attributes, self.ek_attributes = key_attributes, ek_attributes
        self.key_policy = key_policy
        self.ek_certificate, self.chain = ek_certificate, chain
        self.answer = None  # a test may replace the secret returned

    def close(self) -> None:
        pass

    def new_key(self) -> SoftKey:
        key = pki.new_key()
        return SoftKey(pki.key_pem(key), ecc_area(key.public_key(), self.key_attributes,
                                                  policy=self.key_policy),
                       key.public_key(), key)

    def csr(self, key: SoftKey, machine: str) -> str:
        return pki.csr_pem(pki.new_csr(key.handle, machine)).decode("ascii")

    def ek_area(self) -> bytes:
        if isinstance(self.ek_key, rsa.RSAPrivateKey):
            return rsa_ek_area(self.ek_key.public_key(), self.ek_attributes)
        return ecc_area(self.ek_key.public_key(), self.ek_attributes, symmetric=True,
                        policy=EK_POLICY)

    def endorsement(self) -> SoftEndorsement:
        material = {"ek_public": attest.encode(self.ek_area())}
        if self.ek_certificate is not None:
            material["ek_certificate"] = attest.encode(self.ek_certificate)
            material["chain"] = [attest.encode(c) for c in self.chain]
        return SoftEndorsement("soft", material, pki.spki_sha256(self.ek_key.public_key()),
                               "a test CA" if self.ek_certificate else None)

    def activate(self, key: SoftKey, endorsement, challenge: dict) -> bytes:
        if self.answer is not None:
            return self.answer
        blob = base64.b64decode(challenge["credential_blob"])
        encrypted = base64.b64decode(challenge["encrypted_secret"])
        inner = _Reader(blob).sized()
        reader = _Reader(inner)
        integrity = reader.sized()
        identity = inner[reader.pos:]
        sealed = _Reader(encrypted).sized()
        if isinstance(self.ek_key, rsa.RSAPrivateKey):
            seed = self.ek_key.decrypt(sealed, padding.OAEP(
                mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(),
                label=b"IDENTITY\0"))
        else:
            point = _Reader(sealed)
            qx, qy = point.sized(), point.sized()
            ephemeral = ec.EllipticCurvePublicNumbers(
                int.from_bytes(qx, "big"), int.from_bytes(qy, "big"), ec.SECP256R1()
            ).public_key()
            z = self.ek_key.exchange(ec.ECDH(), ephemeral)
            ek_x = self.ek_key.public_key().public_numbers().x.to_bytes(32, "big")
            seed = attest.kdfe(attest.ALG_SHA256, z, b"IDENTITY", qx, ek_x, 256)
        name = attest.parse_public(key.public_area).name
        hmac_key = attest.kdfa(attest.ALG_SHA256, seed, b"INTEGRITY", b"", b"", 256)
        if not hmac.compare_digest(hmac.new(hmac_key, identity + name, hashlib.sha256).digest(),
                                   integrity):
            raise TpmError("the TPM refused the hub's challenge: integrity check failed")
        sym = attest.kdfa(attest.ALG_SHA256, seed, b"STORAGE", name, b"", 128)
        decryptor = Cipher(algorithms.AES(sym), CFB(b"\0" * 16)).decryptor()
        plain = decryptor.update(identity) + decryptor.finalize()
        return _Reader(plain).sized()
