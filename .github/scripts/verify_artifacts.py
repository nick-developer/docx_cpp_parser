#!/usr/bin/env python3
"""Fail the release if the wheel matrix is incomplete.

cibuildwheel does not treat an unmatched build selector as an error — it simply
builds nothing for it.  That is how a release can list ``cp314-*`` and ship no
3.14 wheels at all, with a green tick on every job.  The only way to catch it is
to state what must exist and check.

Run in CI by ``.github/workflows/python-publish.yml``, and locally against a
``dist/`` directory built by hand::

    EXPECTED_PYTHON_TAGS="cp39 cp310 cp311 cp312 cp313 cp314" \\
        python .github/scripts/verify_artifacts.py

Exits 0 when every expected (Python version × platform) wheel is present, the
sdist is present, and — on a release run — every artifact's version matches the
release tag.  Exits 1 with a readable table otherwise.
"""

from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

DIST = Path(os.environ.get("DIST_DIR", "dist"))

#: Platform families a complete release must cover, matched against the wheel's
#: platform tag.  Keep in step with the CIBW_ARCHS_* settings in the workflow:
#: adding an architecture there without adding it here means it is built but
#: never checked.
PLATFORMS: Dict[str, re.Pattern[str]] = {
    "linux x86_64": re.compile(r"manylinux.*_x86_64"),
    "macOS x86_64": re.compile(r"macosx_.*_x86_64"),
    "macOS arm64": re.compile(r"macosx_.*_arm64"),
    "Windows AMD64": re.compile(r"win_amd64"),
}

_VERSION_RE = re.compile(r"^[A-Za-z0-9_.]+?-(\d[^-]*)-")


def wheel_facts(name: str) -> Tuple[str, str, str]:
    """Return ``(version, python_tag, platform_tag)`` for a wheel filename.

    Wheel names are ``{distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl``
    so the interesting fields are read from the end, where the optional build
    tag cannot shift them.
    """
    stem = name[: -len(".whl")]
    parts = stem.split("-")
    if len(parts) < 5:
        raise ValueError(f"not a wheel filename: {name}")
    return parts[1], parts[-3], parts[-1]


def main() -> int:
    expected_tags: List[str] = os.environ.get(
        "EXPECTED_PYTHON_TAGS", "cp39 cp310 cp311 cp312 cp313 cp314"
    ).split()

    if not DIST.is_dir():
        print(f"FAIL: no {DIST}/ directory to check", file=sys.stderr)
        return 1

    wheels = sorted(DIST.glob("*.whl"))
    sdists = sorted(DIST.glob("*.tar.gz"))

    print(f"Checking {len(wheels)} wheel(s) and {len(sdists)} sdist(s) in {DIST}/\n")

    found: Dict[str, Set[str]] = defaultdict(set)
    versions: Set[str] = set()
    unknown: List[str] = []

    for wheel in wheels:
        version, python_tag, platform_tag = wheel_facts(wheel.name)
        versions.add(version)
        matched = False
        for label, pattern in PLATFORMS.items():
            if pattern.search(platform_tag):
                found[python_tag].add(label)
                matched = True
        if not matched:
            # Not a failure on its own — it means the build produced something
            # this check does not know about, which is worth saying out loud.
            unknown.append(wheel.name)

    for sdist in sdists:
        stem = sdist.name[: -len(".tar.gz")]
        match = _VERSION_RE.match(stem + "-")
        if match:
            versions.add(match.group(1))

    # ── The matrix table ────────────────────────────────────────────────────
    labels = list(PLATFORMS)
    width = max(len(tag) for tag in expected_tags) + 2
    header = "".ljust(width) + "  ".join(label.center(14) for label in labels)
    print(header)
    print("-" * len(header))

    missing: List[str] = []
    for tag in expected_tags:
        cells = []
        for label in labels:
            present = label in found.get(tag, set())
            cells.append(("ok" if present else "MISSING").center(14))
            if not present:
                missing.append(f"{tag} / {label}")
        print(tag.ljust(width) + "  ".join(cells))

    extra = sorted(set(found) - set(expected_tags))
    if extra:
        print(f"\nNote: also built, but not required: {', '.join(extra)}")
    if unknown:
        print(f"\nNote: unrecognised platform tags: {', '.join(unknown)}")

    problems: List[str] = []
    if missing:
        problems.append(f"{len(missing)} missing wheel(s): " + "; ".join(missing))
    if not sdists:
        problems.append("no source distribution was built")

    # ── Version agreement ───────────────────────────────────────────────────
    if len(versions) > 1:
        problems.append(f"artifacts disagree on the version: {sorted(versions)}")

    release_tag = os.environ.get("RELEASE_TAG", "").strip()
    if os.environ.get("EVENT_NAME") == "release" and release_tag:
        # Tags are conventionally "v1.4.0"; the artifact version is "1.4.0".
        wanted = release_tag[1:] if release_tag.startswith("v") else release_tag
        if versions and wanted not in versions:
            problems.append(
                f"release tag {release_tag!r} does not match the built version "
                f"{sorted(versions)} — pyproject.toml was probably not bumped"
            )

    if problems:
        print("\n" + "\n".join(f"FAIL: {problem}" for problem in problems), file=sys.stderr)
        return 1

    version = sorted(versions)[0] if versions else "?"
    print(f"\nOK: version {version}, every expected wheel present, sdist present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
