#!/usr/bin/env python3
"""SABnzbd post-processing script: unpack archives left behind by deobfuscation.

Obfuscated posts arrive without a file extension, so SABnzbd's unpack stage
finds nothing to extract. "Deobfuscate final filenames" then detects the file
signature and appends ``.7z``/``.zip`` *after* unpacking has finished, leaving
an archive in the final folder that Sonarr/Radarr cannot import.

This script runs after SABnzbd's own post-processing. For every archive left at
the top level of the job folder it:

1. lists the members and refuses absolute or ``..`` paths,
2. tests the archive (``7z t``),
3. extracts it in place (``7z x``, never overwriting existing files),
4. verifies every listed member now exists,
5. deletes the archive (and any ``.7z.NNN`` volumes) only after 1-4 succeed.

Encrypted archives fail the test (a dummy password suppresses the prompt).
Any failure exits non-zero, so SABnzbd marks the job failed and the *arr apps
see a failed download instead of a silent orphan.

Interface: SABnzbd post-processing script contract, argv[1] = final job folder,
argv[7] = SABnzbd post-processing status (0 = OK). Also honours
SAB_COMPLETE_DIR / SAB_PP_STATUS. The last line printed is shown in History.

Deployed: CT114 shanlieve, /opt/sabnzbd/scripts/unpack-leftover-7z.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Final

FIRST_VOLUME_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<base>.+\.7z)\.0*1$", re.IGNORECASE)
ARCHIVE_RE: Final[re.Pattern[str]] = re.compile(r"\.(?:7z|zip)$|\.7z\.0*1$", re.IGNORECASE)
DUMMY_PASSWORD: Final[str] = "_sabpp_no_password_"
TIMEOUT_S: Final[int] = 3 * 60 * 60
MAX_ROUNDS: Final[int] = 3


class PPError(Exception):
    """An archive could not be processed safely."""


def find_7z() -> str:
    for name in ("7zz", "7z", "7za"):
        path = shutil.which(name)
        if path is not None:
            return path
    raise PPError("no 7-Zip binary found (7zz/7z/7za)")


def run_7z(binary: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [binary, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PPError(f"7z {args[0]} timed out after {TIMEOUT_S}s") from exc


def last_error(result: subprocess.CompletedProcess[str]) -> str:
    lines = [ln.strip() for ln in (result.stderr + result.stdout).splitlines() if ln.strip()]
    return lines[-1] if lines else f"exit {result.returncode}"


def find_candidates(job_dir: Path, done: set[Path]) -> list[Path]:
    return sorted(
        p
        for p in job_dir.iterdir()
        if p.is_file() and ARCHIVE_RE.search(p.name) and p not in done
    )


def volumes_of(archive: Path) -> list[Path]:
    match = FIRST_VOLUME_RE.match(archive.name)
    if match is None:
        return [archive]
    vol_re = re.compile(re.escape(match.group("base")) + r"\.\d+", re.IGNORECASE)
    return sorted(p for p in archive.parent.iterdir() if p.is_file() and vol_re.fullmatch(p.name))


def list_members(binary: str, archive: Path) -> list[str]:
    result = run_7z(binary, ["l", "-slt", f"-p{DUMMY_PASSWORD}", str(archive)])
    if result.returncode != 0:
        raise PPError(f"{archive.name}: list failed: {last_error(result)}")
    members: list[str] = []
    in_entries = False
    for line in result.stdout.splitlines():
        if line.startswith("----------"):
            in_entries = True
        elif in_entries and line.startswith("Path = "):
            members.append(line.removeprefix("Path = "))
    if not members:
        raise PPError(f"{archive.name}: archive is empty")
    return members


def is_unsafe(member: str) -> bool:
    path = PurePosixPath(member.replace("\\", "/"))
    return path.is_absolute() or ".." in path.parts


def process_archive(binary: str, archive: Path, job_dir: Path) -> list[str]:
    members = list_members(binary, archive)
    unsafe = [m for m in members if is_unsafe(m)]
    if unsafe:
        raise PPError(f"{archive.name}: unsafe member path {unsafe[0]!r}")

    clashes = [m for m in members if (job_dir / m).is_file()]
    if clashes:
        raise PPError(f"{archive.name}: member already exists, archive kept: {clashes[0]!r}")

    test = run_7z(binary, ["t", f"-p{DUMMY_PASSWORD}", "-bd", str(archive)])
    if test.returncode != 0:
        raise PPError(f"{archive.name}: test failed (encrypted or corrupt): {last_error(test)}")

    extract = run_7z(
        binary,
        ["x", "-y", "-aos", f"-p{DUMMY_PASSWORD}", "-bd", f"-o{job_dir}", str(archive)],
    )
    if extract.returncode != 0:
        raise PPError(f"{archive.name}: extract failed: {last_error(extract)}")

    missing = [m for m in members if not (job_dir / m).exists()]
    if missing:
        raise PPError(f"{archive.name}: {len(missing)} member(s) missing after extract, e.g. {missing[0]!r}")

    for vol in volumes_of(archive):
        vol.unlink()
    return members


def main(argv: list[str]) -> int:
    job_dir_raw = argv[1] if len(argv) > 1 else os.environ.get("SAB_COMPLETE_DIR", "")
    status_raw = (argv[7] if len(argv) > 7 else os.environ.get("SAB_PP_STATUS", "0")).strip()

    if not job_dir_raw:
        print("FAILED: no job directory supplied")
        return 1
    if status_raw not in ("", "0"):
        print(f"Skipped: SABnzbd post-processing status {status_raw}")
        return 0

    job_dir = Path(job_dir_raw)
    if not job_dir.is_dir():
        print(f"FAILED: not a directory: {job_dir}")
        return 1

    done: set[Path] = set()
    extracted = 0
    try:
        for _ in range(MAX_ROUNDS):
            candidates = find_candidates(job_dir, done)
            if not candidates:
                break
            binary = find_7z()
            for archive in candidates:
                members = process_archive(binary, archive, job_dir)
                done.add(archive)
                extracted += 1
                print(f"Unpacked {archive.name} -> {len(members)} item(s)")
    except PPError as exc:
        print(f"FAILED: {exc}")
        return 1
    except OSError as exc:
        print(f"FAILED: {exc.__class__.__name__}: {exc}")
        return 1

    print(f"OK: unpacked {extracted} leftover archive(s)" if extracted else "OK: no leftover archives")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
