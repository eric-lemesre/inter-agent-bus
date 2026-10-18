"""TPM chip, machine side — the key of `iab join --tpm`, generated inside
the TPM 2.0 chip and never leaving it; the endorsement material and the
credential activation the hub asks for (attest.py is the other side).
Signing TLS handshakes with that key afterwards is tpmtls.py's job.

What this machine keeps (machine.key) is a TSS2 PEM: the key wrapped by
the chip, useless anywhere else — copying the disk or a backup yields
nothing.

Linux first. Needs, besides the `[hub]` extra:
  - tpm2-pytss to pair (extra `[tpm]`, or the python3-tpm2-pytss package),
  - tpm2-openssl for every process that talks to the hub (TLS),
  - read-write access to /dev/tpmrm0 (the `tss` group on Debian).
The bus clients themselves need no tpm2-pytss: only the provider.
"""
from __future__ import annotations

import base64
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import AuthorityInformationAccessOID

try:  # installed package (`iab` maps to this directory)
    from . import attest, pki
    from .tpmtls import DEVICE, TpmError, check_device, enable_provider
except ImportError:  # bare script usage
    import attest
    import pki
    from tpmtls import DEVICE, TpmError, check_device, enable_provider

AIA_TIMEOUT = 10
MAX_AIA = 64 * 1024
# Standard EK templates (TCG EK Credential Profile), certified one first.
EK_TYPES = ("EK-RSA2048", "EK-ECC256")


@dataclass
class MachineKey:
    pem: bytes            # TSS2 PEM, what machine.key holds
    public_area: bytes    # marshaled TPM2B_PUBLIC
    public_key: object    # cryptography public key
    handle: object = None  # loaded in the chip (ESYS_TR) while pairing


@dataclass
class Endorsement:
    ek_type: str
    material: dict        # sent to the hub (ek_public, ek_certificate, chain)
    fingerprint: str      # sha256 of the EK's SubjectPublicKeyInfo
    issuer: str | None    # who certified it, when the chip carries a certificate
    template: object = field(default=None, repr=False)


def _issuers(cert_der: bytes) -> list[bytes]:
    """The intermediate certificates above an EK certificate, fetched from
    its Authority Information Access — the hub ships only the roots. Best
    effort: what cannot be fetched is left to the hub's own CA folder."""
    chain: list[bytes] = []
    try:
        current = x509.load_der_x509_certificate(cert_der)
    except ValueError:
        return chain
    for _ in range(attest.MAX_CHAIN):
        if current.subject == current.issuer:
            break
        try:
            aia = current.extensions.get_extension_for_class(x509.AuthorityInformationAccess)
        except x509.ExtensionNotFound:
            break
        urls = [d.access_location.value for d in aia.value
                if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS
                and isinstance(d.access_location, x509.UniformResourceIdentifier)
                and d.access_location.value.lower().startswith(("http://", "https://"))]
        if not urls:
            break
        try:
            with urllib.request.urlopen(urls[0], timeout=AIA_TIMEOUT) as answer:
                data = answer.read(MAX_AIA + 1)
            if len(data) > MAX_AIA:
                break
            issuer = attest.load_certificates(data)[0]
        except (OSError, ValueError, IndexError):
            break
        if issuer.subject == issuer.issuer:
            break  # a root: the hub has it or will not take it from here
        chain.append(issuer.public_bytes(serialization.Encoding.DER))
        current = issuer
    return chain


class Chip:
    """The TPM of this machine, through tpm2-pytss."""

    def __init__(self, device: Path = DEVICE) -> None:
        check_device(device)
        # A key this machine could not use for TLS afterwards is not worth
        # pairing: the provider must load before the chip is touched.
        enable_provider()
        try:
            import tpm2_pytss
        except ImportError:
            raise TpmError("pairing with a TPM key needs tpm2-pytss: "
                           "pip install 'inter-agent-bus[tpm]' (or the python3-tpm2-pytss "
                           "package with a venv made with --system-site-packages)") from None
        self._t = tpm2_pytss
        from tpm2_pytss.tsskey import TSSPrivKey
        self._tsskey = TSSPrivKey
        try:
            self.ectx = tpm2_pytss.ESAPI(f"device:{device}")
        except tpm2_pytss.TSS2_Exception as exc:
            raise TpmError(f"cannot talk to the TPM: {exc}") from None
        self._loaded: list = []

    def close(self) -> None:
        for handle in self._loaded:
            try:
                self.ectx.flush_context(handle)
            except self._t.TSS2_Exception:
                pass
        self.ectx.close()

    def new_key(self) -> MachineKey:
        """An ECC P-256 signing key generated inside the chip, under the
        standard storage key (recreated from its template at each load)."""
        tss = self._tsskey.create_ecc(self.ectx)
        area = tss.public.marshal()
        handle = tss.load(self.ectx)
        self._loaded.append(handle)
        return MachineKey(tss.to_pem(), area, attest.parse_public(area).key, handle)

    def csr(self, key: MachineKey, machine: str) -> str:
        t = self._t

        def sign(data: bytes) -> bytes:
            digest = hashes.Hash(hashes.SHA256())
            digest.update(data)
            scheme = t.TPMT_SIG_SCHEME(scheme=t.TPM2_ALG.ECDSA)
            scheme.details.any.hashAlg = t.TPM2_ALG.SHA256
            ticket = t.TPMT_TK_HASHCHECK(tag=t.TPM2_ST.HASHCHECK, hierarchy=t.TPM2_RH.NULL)
            signature = self.ectx.sign(key.handle, t.TPM2B_DIGEST(digest.finalize()),
                                       scheme, ticket)
            r = int.from_bytes(bytes(signature.signature.ecdsa.signatureR), "big")
            s = int.from_bytes(bytes(signature.signature.ecdsa.signatureS), "big")
            return encode_dss_signature(r, s)

        return pki.csr_pem(pki.signed_csr(key.public_key, machine, sign)).decode("ascii")

    def endorsement(self, fetch=_issuers) -> Endorsement:
        """The chip's endorsement key: the first standard one that carries
        a manufacturer certificate, else the RSA one alone (a virtual TPM,
        trusted by fingerprint)."""
        from tpm2_pytss.utils import NVReadEK, create_ek_template
        found = []
        for ek_type in EK_TYPES:
            cert, template = create_ek_template(ek_type, NVReadEK(self.ectx))
            found.append((ek_type, cert, template))
            if cert:
                break
        ek_type, cert, template = next((f for f in found if f[1]), found[0])
        handle, public, _, _, _ = self.ectx.create_primary(
            None, template, self._t.ESYS_TR.ENDORSEMENT)
        self.ectx.flush_context(handle)
        area = public.marshal()
        material: dict = {"ek_public": attest.encode(area)}
        issuer = None
        if cert:
            cert = bytes(cert)
            material["ek_certificate"] = attest.encode(cert)
            material["chain"] = [attest.encode(c) for c in fetch(cert)]
            try:
                issuer = attest.issuer_name(x509.load_der_x509_certificate(cert))
            except ValueError:
                issuer = "an unreadable certificate"
        fingerprint = pki.spki_sha256(attest.parse_public(area).key)
        return Endorsement(ek_type, material, fingerprint, issuer, template)

    def activate(self, key: MachineKey, endorsement: Endorsement, challenge: dict) -> bytes:
        """TPM2_ActivateCredential: the secret the hub sealed for this
        chip's EK and this key's name."""
        t = self._t
        try:
            blob, _ = t.TPM2B_ID_OBJECT.unmarshal(
                base64.b64decode(str(challenge.get("credential_blob", "")), validate=True))
            secret, _ = t.TPM2B_ENCRYPTED_SECRET.unmarshal(
                base64.b64decode(str(challenge.get("encrypted_secret", "")), validate=True))
        except (ValueError, TypeError) as exc:
            raise TpmError(f"the hub sent an unusable challenge ({exc})") from None
        ek, _, _, _, _ = self.ectx.create_primary(None, endorsement.template,
                                                  t.ESYS_TR.ENDORSEMENT)
        session = self.ectx.start_auth_session(
            t.ESYS_TR.NONE, t.ESYS_TR.NONE, t.TPM2_SE.POLICY,
            t.TPMT_SYM_DEF(algorithm=t.TPM2_ALG.NULL), t.TPM2_ALG.SHA256)
        try:
            # The standard EK policy: authorized by the endorsement hierarchy.
            self.ectx.policy_secret(t.ESYS_TR.ENDORSEMENT, session, b"", b"", b"", 0)
            return bytes(self.ectx.activate_credential(key.handle, ek, blob, secret,
                                                       t.ESYS_TR.PASSWORD, session))
        except t.TSS2_Exception as exc:
            raise TpmError(f"the TPM refused the hub's challenge: {exc}") from None
        finally:
            self.ectx.flush_context(session)
            self.ectx.flush_context(ek)
