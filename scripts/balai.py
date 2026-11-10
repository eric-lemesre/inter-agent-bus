#!/usr/bin/env python3
"""Sweep old Rust build artifacts of this repository, by AGE only.

    python3 scripts/balai.py               # artifacts unused for 7 days
    python3 scripts/balai.py --days 14
    python3 scripts/balai.py --dry-run     # say what would go, delete nothing

The target directory may be shared by every Rust project of the machine
(`target-dir` in ~/.cargo/config.toml): it is read with `cargo metadata`
and only artifacts older than the given age are removed, whatever project
built them. Never the stamp/file mode of cargo-sweep (`--stamp` then
`--file`): it deletes everything one build did not touch, the other
projects' artifacts included. Never `cargo clean` nor a removal of the
directory either.

Missing cargo-sweep (`cargo install cargo-sweep`) is a warning, not a
failure: sweeping is housekeeping, it never blocks anything.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "rust"
DEFAULT_DAYS = 7
# The modes that sweep by reference to one build, never by age.
FORBIDDEN = ("--stamp", "-s", "--file", "-f")


def target_directory(cargo: str, root: Path) -> str:
    """The effective target directory of the workspace (cargo metadata)."""
    done = subprocess.run(
        [cargo, "metadata", "--format-version", "1", "--no-deps",
         "--manifest-path", str(root / "Cargo.toml")],
        capture_output=True, text=True, encoding="utf-8", check=False)
    if done.returncode != 0:
        raise RuntimeError(f"cargo metadata failed: {done.stderr.strip()}")
    return json.loads(done.stdout)["target_directory"]


def sweep_command(cargo: str, root: Path, days: int, dry_run: bool) -> list[str]:
    command = [cargo, "sweep", "--time", str(days)]
    if dry_run:
        command.append("--dry-run")
    command.append(str(root))
    if any(arg in FORBIDDEN for arg in command):  # a guard, not a feature
        raise RuntimeError(f"refused: {command} would sweep by stamp, not by age")
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"remove artifacts unused for this many days (default {DEFAULT_DAYS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be removed, remove nothing")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="the Cargo workspace (default: rust/ of this repository)")
    args = parser.parse_args(argv)
    if args.days < 1:
        print("ERROR: --days must be at least 1.", file=sys.stderr)
        return 2
    cargo = shutil.which("cargo")
    if cargo is None:
        print("WARNING: cargo not found — nothing swept.", file=sys.stderr)
        return 0
    try:
        target = target_directory(cargo, args.root)
    except (RuntimeError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"target directory: {target}")
    if shutil.which("cargo-sweep") is None:
        print("WARNING: cargo-sweep not installed (cargo install cargo-sweep) — "
              "nothing swept.", file=sys.stderr)
        return 0
    return subprocess.run(sweep_command(cargo, args.root, args.days, args.dry_run),
                          check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
