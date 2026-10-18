"""Admin driver of the multi-machine bus — what an admin machine asks the
hub: approve or deny a pairing, invite a machine, list and revoke
machines, trust a TPM's endorsement key, renew this machine's certificate.

Thin over the hub client (remote.py), through the bus listener, so the
hub checks the admin role on the authenticated machine certificate. The
only logic here is the approval dialogue: a person must read the
request and confirm the check words — there is deliberately no
non-interactive approval (machines deployed by Ansible use invitations).
"""
from __future__ import annotations

import json
import os
import sys

try:  # installed package (`iab` maps to this directory)
    from . import registry, remote
except ImportError:  # bare script usage
    import registry
    import remote


def _backend() -> remote.RemoteBackend | str:
    url = os.environ.get("IAB_HUB") or remote.machine_info().get("hub")
    if not url:
        return "ERROR: no hub — set IAB_HUB (this machine must be paired as admin)."
    return remote.RemoteBackend(url)


def call(action: str, body: dict | None = None):
    """One admin action. Returns the result (dict/list/str) or an ERROR line."""
    backend = _backend()
    if isinstance(backend, str):
        return backend
    try:
        status, answer = backend.request("POST", f"/v1/admin/{action}", body or {})
    except remote.HubError as exc:
        return str(exc)
    if status != 200:
        return str(answer.get("error") or f"ERROR: hub answered HTTP {status}.")
    return answer["result"]


def approve(code: str | None, name: str | None, role: str,
            ask=input, say=print) -> int:
    if not sys.stdin.isatty() and ask is input:
        say("ERROR: approval is interactive on purpose — run `iab approve` in a terminal "
            "(use an invitation for unattended machines).")
        return 1
    code = code or ask("Code: ")
    found = call("pairing/lookup", {"user_code": code})
    if isinstance(found, str):
        say(found)
        return 1
    shown = {k: registry.printable(v) for k, v in found.items() if k != "inherits"}  # never raw to a terminal
    say(f"Request from '{shown['name']}' — {shown['platform'] or 'unknown system'} — "
        f"{shown['source']} — key: {shown['key_kind']}")
    if found.get("attestation"):
        say(f"TPM key attested: {registry.printable(found['attestation'], 200)}")
    if found.get("inherits"):
        inherited = ", ".join(registry.printable(a) for a in found["inherits"])
        say(f"This name was paired before: the machine would inherit {inherited}")
    say(f"Check words: {shown['words']}")
    final = (name or found["name"]).strip().lower()
    answer = ask(f"Are the four words identical to those shown on {shown['name']}? "
                 f"Pair it as '{final}' ({role}) [y/N] ").strip().lower()
    if answer not in ("y", "yes", "o", "oui"):
        denied = call("pairing/deny", {"user_code": code})
        say("Not approved — request denied." if not str(denied).startswith("ERROR")
            else str(denied))
        return 1
    done = call("pairing/approve", {"user_code": code, "name": final, "role": role})
    if isinstance(done, str):
        say(done)
        return 1
    say(f"Machine '{done['name']}' paired (certificate until {done['expires_at'][:10]}).")
    return 0


def show(result) -> int:
    if isinstance(result, str):
        print(result)
        return 1 if result.startswith("ERROR") else 0
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def renew(force: bool) -> int:
    backend = _backend()
    if isinstance(backend, str):
        print(backend)
        return 1
    try:
        out = backend.renew(force=force)
    except remote.HubError as exc:
        out = str(exc)
    print(out)
    return 1 if out.startswith("ERROR") else 0
