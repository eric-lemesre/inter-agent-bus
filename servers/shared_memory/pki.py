"""Certificates of the multi-machine bus — the bus CA, the hub's server
certificate, machine keys and certificates.

Trust model:
  - the hub holds a private CA (EC P-256) that signs two kinds of
    certificates: machine certificates (client authentication, short-lived,
    renewed automatically) and the server certificate of the bus listener;
  - a client trusts ONLY that CA on the bus listener (pinned at pairing):
    a TLS-inspecting proxy cannot present a certificate chaining to it, so
    the handshake fails before a single byte of payload leaves the machine;
  - the private key of a machine is generated on the machine and never
    travels: only a certificate signing request does.

Needs the `cryptography` package (extra `[hub]`); the core never imports
this module.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import os
from pathlib import Path

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ImportError as exc:  # pragma: no cover — depends on the install
    raise ImportError(
        "the multi-machine bus needs the `cryptography` package: "
        "pip install 'inter-agent-bus[hub]'"
    ) from exc

CA_DAYS = 3650
SERVER_DAYS = 365
MACHINE_DAYS = 30
# A machine certificate is renewed when less than this remains.
RENEW_BEFORE = _dt.timedelta(days=10)
_BACKDATE = _dt.timedelta(minutes=5)  # tolerate small clock skews


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def write_private(path: Path, data: bytes) -> None:
    """Write a secret file owner-only from its creation (no window where
    another user could read it)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    if os.name == "posix":
        os.chmod(path, 0o600)


def write_public(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def load_key(path: Path) -> ec.EllipticCurvePrivateKey:
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def cert_der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(Path(path).read_bytes())


def load_cert_bytes(data: bytes) -> x509.Certificate:
    return x509.load_pem_x509_certificate(data)


def spki_sha256(public_key) -> str:
    """Fingerprint of a public key (SubjectPublicKeyInfo, DER, SHA-256)."""
    der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(der).hexdigest()


def cert_sha256(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def common_name(cert: x509.Certificate) -> str:
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attrs[0].value) if attrs else ""


def not_after(cert: x509.Certificate) -> _dt.datetime:
    return cert.not_valid_after_utc


def needs_renewal(cert: x509.Certificate, margin_days: float | None = None) -> bool:
    margin = RENEW_BEFORE if margin_days is None else _dt.timedelta(days=margin_days)
    return not_after(cert) - _utcnow() < margin


def _name(cn: str, unit: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "inter-agent-bus"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, unit),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])


def new_ca(label: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """The bus CA: signs machine certificates and the bus server
    certificate, nothing else (path length 0)."""
    key = new_key()
    now = _utcnow()
    name = _name(f"{label} bus CA", "ca")
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + _dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _leaf(
    ca_key, ca_cert: x509.Certificate, public_key, subject: x509.Name, days: int,
    usage, san: list | None = None,
) -> x509.Certificate:
    now = _utcnow()
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(min(now + _dt.timedelta(days=days), ca_cert.not_valid_after_utc))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
    )
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return builder.sign(ca_key, hashes.SHA256())


def _san_entry(name: str):
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


def server_cert(
    ca_key, ca_cert: x509.Certificate, names: list[str]
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """Server certificate of the bus listener for the given DNS names or
    IP addresses (the first one is the common name)."""
    key = new_key()
    cert = _leaf(ca_key, ca_cert, key.public_key(), _name(names[0], "hub"), SERVER_DAYS,
                 ExtendedKeyUsageOID.SERVER_AUTH, [_san_entry(n) for n in names])
    return key, cert


def new_csr(key, machine: str) -> x509.CertificateSigningRequest:
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(_name(machine, "machine"))
        .sign(key, hashes.SHA256())
    )


def csr_pem(csr: x509.CertificateSigningRequest) -> bytes:
    return csr.public_bytes(serialization.Encoding.PEM)


def load_csr(data: bytes) -> x509.CertificateSigningRequest:
    """Parse a CSR and check its self-signature (proof of key possession)."""
    csr = x509.load_pem_x509_csr(data)
    if not csr.is_signature_valid:
        raise ValueError("CSR signature is invalid")
    if not isinstance(csr.public_key(), ec.EllipticCurvePublicKey):
        raise ValueError("CSR key must be an elliptic-curve key")
    return csr


def machine_cert(
    ca_key, ca_cert: x509.Certificate, public_key, machine: str, days: int = MACHINE_DAYS
) -> x509.Certificate:
    """Client certificate of a machine; the subject comes from the hub's
    decision (the approved name), never from the request."""
    return _leaf(ca_key, ca_cert, public_key, _name(machine, "machine"), days,
                 ExtendedKeyUsageOID.CLIENT_AUTH)
