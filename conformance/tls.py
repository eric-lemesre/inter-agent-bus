#!/usr/bin/env python3
"""TLS and authentication of the native client against the bench of
scripts/pg_tls.py — what a remote PostgreSQL bus must accept and refuse.

    url=$(python3 scripts/pg_tls.py start DIR)
    IAB_TEST_POSTGRES=$url python3 conformance/tls.py DIR /path/to/iab

Every check runs `iab state` (it connects, creates the schema, reads) with
a variant of the bench URL. The Python implementation goes through libpq,
whose refusals the bench itself checks before handing out its URL; this
script judges the native client, which reads libpq's TLS parameters on
its own (CONTRAT.md, § 1). A password never appears on a command line:
it travels through PGPASSWORD or a pgpass file.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("ok — " if ok else "FAIL: ") + label + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def variant(url: str, user: str | None = None, host: str | None = None, drop=(), **params: str) -> str:
    """The bench URL with another user or host, parameters dropped or set."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k not in drop and k not in params]
    query += list(params.items())
    netloc = f"{user or parts.username}@{host or parts.hostname}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path,
                                    urllib.parse.urlencode(query, quote_via=urllib.parse.quote), ""))


def main() -> int:
    if len(sys.argv) != 3 or not os.environ.get("IAB_TEST_POSTGRES"):
        print(__doc__)
        return 2
    bench, iab = Path(sys.argv[1]), sys.argv[2]
    base = os.environ["IAB_TEST_POSTGRES"]
    password = (bench / "password").read_text(encoding="ascii")
    import psycopg  # the bench's own dependency
    schema = f"tls_{os.getpid()}_{time.time_ns() % 10**9}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    base = variant(base, options=f"-csearch_path={schema}")
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("IAB_", "ORCHESTRATOR_", "PG"))}

    def state(url: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([iab, "state"], capture_output=True, timeout=60,
                              env={**clean, "IAB_DB": url, **env})

    def accepted(label: str, url: str, **env: str) -> None:
        done = state(url, **env)
        check(label, done.returncode == 0 and done.stdout.lstrip().startswith(b"{"),
              done.stdout.decode("utf-8", "replace")[:300])

    def refused(label: str, url: str, **env: str) -> None:
        done = state(url, **env)
        out = done.stdout.decode("utf-8", "replace")
        check(label, done.returncode != 0 and out.startswith("ERROR"), out[:300])
        check(f"{label}: the password stays out of the answer", password not in out)

    accepted("verify-full with a client certificate", base)
    refused("clear text (sslmode=disable) refused by the server", variant(base, sslmode="disable"))
    refused("no sslmode (libpq's prefer) refused by the client", variant(base, drop=("sslmode",)))
    refused("sslmode=prefer refused by the client", variant(base, sslmode="prefer"))
    refused("a foreign authority refused",
            variant(base, sslrootcert=str(bench / "foreign.crt")))
    refused("a host name absent from the certificate refused (verify-full)",
            variant(base, host="127.0.0.1"))
    accepted("the same host accepted by verify-ca (chain only)",
             variant(base, host="127.0.0.1", sslmode="verify-ca"))
    # require: encrypted; the chain is checked when a root file is given
    # (as libpq does), the host name never.
    accepted("require with the bench's root: chain checked, accepted",
             variant(base, sslmode="require"))
    accepted("require ignores the host name",
             variant(base, host="127.0.0.1", sslmode="require"))
    refused("require with a foreign root refused",
            variant(base, sslmode="require", sslrootcert=str(bench / "foreign.crt")))
    accepted("require without any root file: encrypted, server not authenticated",
             variant(base, sslmode="require", drop=("sslrootcert",)), HOME=str(bench), APPDATA=str(bench))
    refused("no client certificate refused by the server",
            variant(base, drop=("sslcert", "sslkey")))

    pw = variant(base, user="iab_pw", drop=("sslcert", "sslkey"))
    accepted("password (SCRAM) from PGPASSWORD", pw, PGPASSWORD=password)
    refused("a wrong password refused", pw, PGPASSWORD=password + "x")
    with tempfile.TemporaryDirectory() as tmp:
        passfile = Path(tmp) / "pgpass"
        port = urllib.parse.urlsplit(base).port
        passfile.write_text(f"localhost:{port}:iab_test:iab_pw:{password}\n", encoding="ascii")
        if os.name == "posix":
            passfile.chmod(0o600)
        accepted("password from a pgpass file (PGPASSFILE)", pw, PGPASSFILE=str(passfile))
    refused("password role without any password refused", pw)

    with psycopg.connect(os.environ["IAB_TEST_POSTGRES"], autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    print(f"tls: {'OK' if not failures else f'{len(failures)} FAILURE(S)'}.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
