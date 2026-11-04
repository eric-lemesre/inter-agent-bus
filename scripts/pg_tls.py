#!/usr/bin/env python3
"""A throwaway PostgreSQL server that only accepts TLS with a client
certificate — the test bench of a bus shared through a remote database.

    python3 scripts/pg_tls.py start DIR   # prints the IAB_TEST_POSTGRES URL
    python3 scripts/pg_tls.py stop DIR

`start` generates a test authority, a server certificate for
`localhost` and a client certificate for the role `iab`, creates a
cluster in DIR, starts it on 127.0.0.1 and creates the empty database
`iab_test` owned by `iab`. The role `iab` may connect only over TLS
(`hostssl … cert`): a clear-text connection, a foreign authority or a
host name absent from the server certificate are refused — `start`
checks all three before printing the URL, so a bench that would accept
them never reaches the tests.

The URL uses `sslmode=verify-full`: chain AND host name verified, the
setting a real deployment must use. Needs the PostgreSQL server binaries
(PGBIN, `pg_config --bindir`, or /usr/lib/postgresql/*/bin), the
`cryptography` package (extra `[hub]`) and psycopg (extra `[postgres]`).
Pure Python: runs the same on Linux, macOS and Windows.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROLE = "iab"
DATABASE = "iab_test"


def _bindir() -> Path:
    candidates = []
    if os.environ.get("PGBIN"):
        candidates.append(Path(os.environ["PGBIN"]))
    pg_config = shutil.which("pg_config")
    if pg_config:
        out = subprocess.run([pg_config, "--bindir"], capture_output=True, text=True)
        if out.returncode == 0:
            candidates.append(Path(out.stdout.strip()))
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin"),
                         key=lambda p: int(p.parent.name) if p.parent.name.isdigit() else 0,
                         reverse=True)
    exe = "initdb.exe" if os.name == "nt" else "initdb"
    for candidate in candidates:
        if (candidate / exe).exists():
            return candidate
    raise SystemExit("ERROR: PostgreSQL server binaries not found (set PGBIN).")


def _write_private(path: Path, data: bytes) -> None:
    """Key files: owner-only, as both libpq and the server demand on POSIX."""
    path.write_bytes(data)
    if os.name == "posix":
        path.chmod(0o600)


def _certificates(directory: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    pem = serialization.Encoding.PEM

    def key_bytes(key) -> bytes:
        return key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())

    def authority(name: str):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        cert = (x509.CertificateBuilder()
                .subject_name(subject).issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=2))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(False, False, False, False, False, True, True,
                                             False, False), critical=True)
                .sign(key, hashes.SHA256()))
        return key, cert

    def leaf(ca_key, ca_cert, common_name: str, san, usage) -> tuple[bytes, bytes]:
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
                   .issuer_name(ca_cert.subject)
                   .public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(minutes=5))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([usage]), critical=False))
        if san:
            builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
        cert = builder.sign(ca_key, hashes.SHA256())
        return cert.public_bytes(pem), key_bytes(key)

    from cryptography.x509.oid import ExtendedKeyUsageOID

    ca_key, ca_cert = authority("iab test authority")
    (directory / "root.crt").write_bytes(ca_cert.public_bytes(pem))
    # The server certificate names `localhost` only, not 127.0.0.1: a
    # connection to the bare address must fail the host name check.
    cert, key = leaf(ca_key, ca_cert, "localhost", [x509.DNSName("localhost")],
                     ExtendedKeyUsageOID.SERVER_AUTH)
    (directory / "server.crt").write_bytes(cert)
    _write_private(directory / "server.key", key)
    cert, key = leaf(ca_key, ca_cert, ROLE, [], ExtendedKeyUsageOID.CLIENT_AUTH)
    (directory / "client.crt").write_bytes(cert)
    _write_private(directory / "client.key", key)
    _, foreign = authority("foreign authority")
    (directory / "foreign.crt").write_bytes(foreign.public_bytes(pem))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _url(directory: Path, port: int, **overrides: str) -> str:
    params = {
        "hostaddr": "127.0.0.1",
        "sslmode": "verify-full",
        "sslrootcert": str(directory / "root.crt"),
        "sslcert": str(directory / "client.crt"),
        "sslkey": str(directory / "client.key"),
        "connect_timeout": "10",
    }
    params.update(overrides)
    host = params.pop("host", "localhost")
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"postgresql://{ROLE}@{host}:{port}/{DATABASE}?{query}"


def _refused(url: str) -> bool:
    import psycopg
    try:
        psycopg.connect(url).close()
    except psycopg.OperationalError:
        return True
    return False


def start(directory: Path) -> str:
    import psycopg

    directory = directory.resolve()
    if (directory / "data").exists():
        raise SystemExit(f"ERROR: {directory / 'data'} exists — stop and remove it first.")
    directory.mkdir(parents=True, exist_ok=True)
    bindir = _bindir()
    _certificates(directory)
    data = directory / "data"
    subprocess.run([str(bindir / "initdb"), "-D", str(data), "-U", "postgres",
                    "--auth=reject", "-E", "UTF8", "--no-locale"],
                   check=True, capture_output=True)
    port = _free_port()
    for name in ("server.crt", "server.key", "root.crt"):
        shutil.copy2(directory / name, data / name)
    if os.name == "posix":
        (data / "server.key").chmod(0o600)
    with (data / "postgresql.conf").open("a", encoding="utf-8") as conf:
        conf.write(
            "\n# pg_tls.py\n"
            f"port = {port}\n"
            "listen_addresses = '127.0.0.1'\n"
            "unix_socket_directories = ''\n"
            "ssl = on\n"
            "ssl_cert_file = 'server.crt'\n"
            "ssl_key_file = 'server.key'\n"
            "ssl_ca_file = 'root.crt'\n"
            "max_connections = 200\n"
        )
    # Bootstrap account over clear-text loopback (test bench only); the bus
    # role over TLS with a client certificate, nothing else.
    (data / "pg_hba.conf").write_text(
        "host    all       postgres 127.0.0.1/32 trust\n"
        f"hostssl {DATABASE} {ROLE}      127.0.0.1/32 cert\n",
        encoding="utf-8",
    )
    subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-l", str(directory / "server.log"),
                    "-w", "-t", "60", "start"], check=True, capture_output=True)
    admin = f"postgresql://postgres@127.0.0.1:{port}/postgres?sslmode=disable"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {ROLE} LOGIN")
        conn.execute(f"CREATE DATABASE {DATABASE} OWNER {ROLE}")
    url = _url(directory, port)
    psycopg.connect(url).close()
    refusals = {
        "clear text": _url(directory, port, sslmode="disable"),
        "foreign authority": _url(directory, port, sslrootcert=str(directory / "foreign.crt")),
        "host name not in the certificate": _url(directory, port, host="127.0.0.1"),
    }
    accepted = [label for label, bad in refusals.items() if not _refused(bad)]
    if accepted:
        stop(directory)
        raise SystemExit(f"ERROR: the bench accepted {', '.join(accepted)}.")
    return url


def stop(directory: Path) -> None:
    data = directory.resolve() / "data"
    if data.exists():
        subprocess.run([str(_bindir() / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                       capture_output=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("start", "stop"):
        print("usage: pg_tls.py start|stop DIR", file=sys.stderr)
        return 2
    command, directory = argv[0], Path(argv[1])
    if command == "start":
        print(start(directory))
    else:
        stop(directory)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
