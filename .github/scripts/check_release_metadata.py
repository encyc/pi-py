#!/usr/bin/env python3
"""Verify a release tag is internally consistent before publishing.

Checks (all must pass):
1. ``--tag`` is a semver tag of the form ``vX.Y.Z``.
2. The tag exists in the local git clone (skipped automatically for fixture
   roots that are not git repositories).
3. Every workspace package under ``packages/*/pyproject.toml`` is on exactly
   the same version as the tag (lockstep versioning; a partial bump silently
   publishes a mixed-version release).
4. ``CHANGELOG.md`` contains a heading for that version.

The fourth check catches "tag exists, but the release notes were never
written" — the same class of failure as the missing GitHub Release.

Exit codes: 0 = OK, 1 = at least one check failed, 2 = could not determine state.

Usage:
    python3 .github/scripts/check_release_metadata.py --tag v0.85.1
    # fixture / offline:
    python3 .github/scripts/check_release_metadata.py --tag v1.3.0 --root /tmp/fixture
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from packages_common import SEMVER_TAG_RE, discover_packages, find_repo_root  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tag", required=True, help="release tag, e.g. v1.3.0")
    parser.add_argument("--root", type=Path, default=None, help="repository (or fixture) root")
    parser.add_argument("--changelog", type=Path, default=None, help="default: <root>/CHANGELOG.md")
    return parser.parse_args()


def tag_exists(root: Path, tag: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"refs/tags/{tag}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def is_git_repo(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def has_changelog_heading(changelog: Path, version: str) -> bool:
    if not changelog.is_file():
        return False
    pattern = re.compile(rf"^##\s+\[?{re.escape(version)}\]?(?:\s|$)", re.MULTILINE)
    return pattern.search(changelog.read_text(encoding="utf-8")) is not None


def main() -> int:
    args = parse_args()
    root = args.root or find_repo_root()
    failures: list[str] = []

    match = SEMVER_TAG_RE.fullmatch(args.tag)
    if not match:
        failures.append(f"tag {args.tag!r} is not a semver tag (expected vX.Y.Z)")
        version = ""
    else:
        version = f"{match.group(1)}.{match.group(2)}.{match.group(3)}"

    if version:
        if is_git_repo(root):
            if not tag_exists(root, args.tag):
                failures.append(f"tag {args.tag!r} does not exist in {root}")
        else:
            print(f"note: {root} is not a git repository; skipping tag-existence check")

        packages = discover_packages(root)
        if not packages:
            failures.append(
                f"no workspace packages found under {root / 'packages'}/*/pyproject.toml"
            )
        for pkg in packages:
            if pkg["version"] != version and not pkg["private"]:
                failures.append(
                    f"{pkg['name']} is at {pkg['version']} but the tag implies {version} "
                    f"({pkg['path'].relative_to(root)})"
                )

        changelog = args.changelog or (root / "CHANGELOG.md")
        if not has_changelog_heading(changelog, version):
            failures.append(f"CHANGELOG.md has no '## {version}' section ({changelog})")

    print(f"Release metadata check: tag={args.tag} root={root}")
    if not failures:
        print("OK: tag, package versions and CHANGELOG are consistent")
        return 0
    for failure in failures:
        print(f"::error::{failure}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
