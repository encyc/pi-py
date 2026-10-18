#!/usr/bin/env python3
"""Fail when a release tag exists but has no published GitHub Release.

Why this exists
---------------
``publish.yml`` is triggered by ``release: published``. In September 2026 the
``v0.85.1`` tag was pushed, but no GitHub Release was ever created, so the
publish workflow never ran and PyPI stayed on ``0.84.1`` for weeks. Nothing in
CI noticed. This script is that missing check.

The rule
--------
Every tag matching ``v<major>.<minor>.<patch>`` must have a corresponding
GitHub Release that is *not a draft*. Tags younger than ``--grace-minutes``
(default 120) are reported as "pending" instead of failing, so the normal
"push tag -> create release a few minutes later" flow keeps working.

Exit codes: 0 = OK, 1 = drift found, 2 = could not determine state.

Usage
-----
    python3 .github/scripts/check_tag_release_consistency.py
    python3 .github/scripts/check_tag_release_consistency.py --grace-minutes 60

Offline / test usage (no git, no gh):

    python3 .github/scripts/check_tag_release_consistency.py \
        --tags-file tags.txt --releases-file releases.txt \
        --now 2026-10-18T00:00:00Z

``tags.txt`` lines: ``v0.85.1`` or ``v0.85.1<TAB>2026-09-08T03:54:47+00:00``
``releases.txt`` lines: ``v0.84.1`` or ``v0.84.1<TAB>draft``
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from packages_common import SEMVER_TAG_RE, find_repo_root, run_git  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None, help="repository root (default: auto-detect)"
    )
    parser.add_argument(
        "--grace-minutes",
        type=int,
        default=120,
        help="tags younger than this are not errors (default: 120)",
    )
    parser.add_argument(
        "--tags-file", type=Path, default=None, help="offline: file with one tag per line"
    )
    parser.add_argument(
        "--releases-file",
        type=Path,
        default=None,
        help="offline: file with one release tag per line",
    )
    parser.add_argument(
        "--now", type=str, default=None, help="offline: ISO-8601 timestamp used as 'now'"
    )
    return parser.parse_args()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def read_tags_from_git(root: Path) -> dict[str, datetime | None]:
    """Return {tag: commit date} for every semver tag in the local clone."""
    tags: dict[str, datetime | None] = {}
    for tag in run_git(root, "tag", "--list").splitlines():
        tag = tag.strip()
        if not SEMVER_TAG_RE.fullmatch(tag):
            continue
        try:
            date_str = run_git(root, "log", "-1", "--format=%cI", tag)
            tags[tag] = _parse_iso(date_str)
        except RuntimeError:
            tags[tag] = None
    return tags


def read_releases_from_gh(root: Path) -> set[str]:
    """Return the set of tag names that have a non-draft GitHub Release."""
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                "repos/{owner}/{repo}/releases?per_page=100",
                "--jq",
                '.[] | "\\(.tag_name)\\t\\(.draft)"',
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - CI always has gh
        raise RuntimeError("gh CLI not found; cannot list GitHub Releases") from exc
    if result.returncode != 0:
        raise RuntimeError(f"gh api failed: {result.stderr.strip()}")
    released: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].strip().lower() != "true":
            released.add(parts[0].strip())
    return released


def read_tags_file(path: Path) -> dict[str, datetime | None]:
    tags: dict[str, datetime | None] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        tag = parts[0].strip()
        if not SEMVER_TAG_RE.fullmatch(tag):
            continue
        tags[tag] = _parse_iso(parts[1]) if len(parts) > 1 and parts[1].strip() else None
    return tags


def read_releases_file(path: Path) -> set[str]:
    released: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        tag = parts[0].strip()
        is_draft = len(parts) > 1 and parts[1].strip().lower() == "draft"
        if not is_draft:
            released.add(tag)
    return released


def main() -> int:
    args = parse_args()
    now = _parse_iso(args.now) if args.now else datetime.now(UTC)
    grace = timedelta(minutes=args.grace_minutes)

    try:
        if args.tags_file and args.releases_file:
            tags = read_tags_file(args.tags_file)
            released = read_releases_file(args.releases_file)
            source = "fixtures"
        else:
            root = args.repo_root or find_repo_root()
            tags = read_tags_from_git(root)
            released = read_releases_from_gh(root)
            source = str(root)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"::error::release guard: cannot determine tag/release state: {exc}")
        return 2

    missing: list[str] = []
    pending: list[str] = []
    for tag, tag_date in sorted(tags.items()):
        if tag in released:
            continue
        if tag_date is not None and tag_date > now - grace:
            pending.append(
                f"{tag} (tagged {tag_date.isoformat()}, within {args.grace_minutes}m grace)"
            )
        else:
            suffix = f"tagged {tag_date.isoformat()}" if tag_date else "tag date unknown"
            missing.append(f"{tag} ({suffix})")

    print(
        f"Release guard [{source}]: {len(tags)} semver tag(s), {len(released)} published release(s)"
    )
    for item in pending:
        print(f"  pending: {item}")

    if not missing:
        print("OK: every release tag has a published GitHub Release")
        return 0

    print("")
    for item in missing:
        print(f"::error::tag {item} has no GitHub Release -> publish.yml will never run for it")
    print("")
    print("How to fix a missing release:")
    print("  gh release create <tag> --title <tag> --notes-file <changelog-section>")
    print("  or: Actions -> 'Publish to PyPI' -> Run workflow -> tag=<tag>")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
