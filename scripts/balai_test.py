#!/usr/bin/env python3
"""Tests of scripts/balai.py against a fake `cargo` on the PATH: the sweep
is by age, never by stamp/file, and a missing cargo-sweep only warns.

    python3 scripts/balai_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BALAI = HERE / "balai.py"
failures: list[str] = []

FAKE_CARGO = """
import json, sys
from pathlib import Path
log = Path(sys.argv[1])
args = sys.argv[2:]
with log.open("a", encoding="utf-8") as out:
    out.write(json.dumps(args) + "\\n")
if args[:1] == ["metadata"]:
    print(json.dumps({"target_directory": "/shared/cargo-target"}))
"""


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("ok — " if ok else "FAIL: ") + label + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def launcher(bindir: Path, name: str, script: Path, log: Path) -> None:
    """An executable `name` on the PATH running `script` with Python."""
    if os.name == "nt":
        (bindir / f"{name}.cmd").write_text(
            f'@"{sys.executable}" "{script}" "{log}" %*\r\n', encoding="utf-8")
    else:
        path = bindir / name
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{log}" "$@"\n',
                        encoding="utf-8")
        path.chmod(0o755)


def run(tmp: Path, *args: str, sweep_installed: bool = True) -> tuple[subprocess.CompletedProcess, list]:
    bindir = tmp / ("bin-sweep" if sweep_installed else "bin-bare")
    log = tmp / f"{bindir.name}.log"
    if not bindir.exists():
        bindir.mkdir()
        fake = tmp / "fake_cargo.py"
        fake.write_text(FAKE_CARGO, encoding="utf-8")
        launcher(bindir, "cargo", fake, log)
        if sweep_installed:
            launcher(bindir, "cargo-sweep", fake, log)
    log.write_text("", encoding="utf-8")
    env = {**os.environ, "PATH": str(bindir)}
    done = subprocess.run([sys.executable, str(BALAI), "--root", str(tmp), *args],
                          capture_output=True, text=True, encoding="utf-8", env=env)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    return done, calls


def sweeps(calls: list) -> list:
    return [c for c in calls if c[:1] == ["sweep"]]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        done, calls = run(tmp, "--dry-run")
        check("the target directory is read with cargo metadata",
              "target directory: /shared/cargo-target" in done.stdout
              and calls and calls[0][0] == "metadata", done.stdout + done.stderr)
        check("a dry run sweeps by age, 7 days by default, on the workspace",
              sweeps(calls) == [["sweep", "--time", "7", "--dry-run", str(tmp)]], str(calls))
        done, calls = run(tmp, "--days", "14")
        check("--days sets the age, without --dry-run",
              done.returncode == 0
              and sweeps(calls) == [["sweep", "--time", "14", str(tmp)]], str(calls))
        flat = [arg for call in calls for arg in call]
        check("never the stamp/file mode",
              not {"--stamp", "-s", "--file", "-f"} & set(flat), str(calls))
        done, calls = run(tmp, "--dry-run", sweep_installed=False)
        check("a missing cargo-sweep warns without failing and sweeps nothing",
              done.returncode == 0 and "cargo-sweep not installed" in done.stderr
              and not sweeps(calls), done.stderr + str(calls))
        done, calls = run(tmp, "--days", "0")
        check("an age under one day is refused", done.returncode != 0 and not calls,
              done.stderr)
    print(f"balai_test: {'OK' if not failures else 'FAIL'}.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
