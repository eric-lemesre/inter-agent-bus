"""TLS with a TPM-held machine key — what every bus client of a machine
paired with `iab join --tpm` needs, and nothing more: recognise the key
file, load OpenSSL's `tpm2` provider (tpm2-openssl) so that Python's ssl
module can sign handshakes with it, one handshake at a time.

Standard library only (ctypes), like remote.py: the bus clients need
neither tpm2-pytss nor cryptography for this. Pairing is tpm.py's job.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import ssl
import threading
from pathlib import Path

KEY_HEADER = b"-----BEGIN TSS2 PRIVATE KEY-----"
DEVICE = Path("/dev/tpmrm0")  # the kernel's resource manager: shared safely

_lock = threading.Lock()
_handshake = threading.Lock()
_loaded = False


class TpmError(RuntimeError):
    pass


def is_tpm_key(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(KEY_HEADER)) == KEY_HEADER
    except OSError:
        return False


def quiet() -> None:
    """The TSS logs every probe of an absent NV index as an error; the
    failures that matter come back as exceptions anyway."""
    os.environ.setdefault("TSS2_LOG", "all+NONE")


def _libcrypto() -> ctypes.CDLL:
    """The libcrypto the ssl module is linked with (a second copy would
    load the provider where ssl never looks)."""
    maps = Path("/proc/self/maps")
    if maps.exists():
        for line in maps.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split(None, 5)
            if len(fields) == 6 and "/libcrypto" in fields[5]:
                return ctypes.CDLL(fields[5])
    found = ctypes.util.find_library("crypto")
    if not found:
        raise TpmError("cannot locate OpenSSL's libcrypto")
    return ctypes.CDLL(found)


def check_device(device: Path = DEVICE) -> None:
    if not device.exists():
        raise TpmError(f"no TPM on this machine ({device} does not exist)")
    if not os.access(device, os.R_OK | os.W_OK):
        raise TpmError(f"this process cannot open {device} — add the user to its group "
                       "(`sudo usermod -aG tss $USER`), then open a new session")


def enable_provider() -> None:
    """Load OpenSSL's `tpm2` provider next to the default one (loading any
    provider explicitly disables the implicit default). Once per process."""
    global _loaded
    check_device()
    with _lock:
        if _loaded:
            return
        if ssl.OPENSSL_VERSION_INFO[0] < 3:
            raise TpmError(f"a TPM key needs OpenSSL 3 (this Python uses {ssl.OPENSSL_VERSION})")
        quiet()
        load = _libcrypto().OSSL_PROVIDER_load
        load.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        load.restype = ctypes.c_void_p
        if not load(None, b"default"):
            raise TpmError("OpenSSL refused to load its default provider")
        if not load(None, b"tpm2"):
            raise TpmError("OpenSSL cannot load the 'tpm2' provider — install tpm2-openssl")
        _loaded = True


def handshake_lock() -> threading.Lock:
    """One TLS handshake at a time per process with a TPM key: the chip
    signs one request at a time, and the provider's session is not shared
    safely between threads."""
    return _handshake
