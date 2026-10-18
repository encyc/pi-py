#!/usr/bin/env python3
"""Verify that every published package is available on PyPI at the expected version.

Used in two places:
* ``.github/workflows/publish.yml`` — after the upload, as the post-publish
  assertion. ``skip-existing: true`` on the upload action makes retries safe,
  but it also means an upload problem can finish "successfully" while PyPI
  keeps serving an older version. This check turns that into a red build.
* manually, to audit the current gap (``--version 0.85.1`` shows what is
  missing before a back-fill release).

Usage:
    python3 .github/scripts/check_pypi_versions.py --version 0.85.1
    python3 .github/scripts/check_pypi_versions.py --version v1.3.0 --retries 12 --delay 15
    python3 .github/scripts/check_pypi_versions.py --list
    # offline / test:
    python3 .github/scripts/check_pypi_versions.py --version 1.0.0 --base-url http://127.0.0.1:8000

Exit codes: 0 = all packages at the expected version, 1 = mismatch, 2 = error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from packages_common import discover_packages, find_repo_root  # noqa: E402

DEFAULT_BASE_URL = "https://pypi.org"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--version", default=None, help="expected version, with or without leading 'v'"
    )
    parser.add_argument(
        "--root", type=Path, default=None, help="repository root (default: auto-detect)"
    )
    parser.add_argument(
        "--packages",
        default=None,
        help="comma-separated package names (default: all public workspace packages)",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help=f"PyPI base URL (default: {DEFAULT_BASE_URL})"
    )
    parser.add_argument(
        "--retries", type=int, default=0, help="extra attempts per package (default: 0)"
    )
    parser.add_argument(
        "--delay", type=float, default=15.0, help="seconds between attempts (default: 15)"
    )
    parser.add_argument(
        "--list", action="store_true", help="print public workspace package names and exit"
    )
    return parser.parse_args()


def latest_on_pypi(base_url: str, package: str) -> str | None:
    """Return the version PyPI serves as latest, or None if not published."""
    url = f"{base_url.rstrip('/')}/pypi/{package}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310 - fixed https/CI URLs
            payload = json.load(response)
        return str(payload["info"]["version"])
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc}") from exc


def main() -> int:
    args = parse_args()
    root = args.root or find_repo_root()
    packages = [pkg for pkg in discover_packages(root) if not pkg["private"]]

    if args.list:
        for pkg in packages:
            print(pkg["name"])
        return 0

    if not args.version:
        print("::error::--version is required unless --list is used")
        return 2
    expected = args.version.lstrip("v")

    if args.packages:
        wanted = {name.strip() for name in args.packages.split(",") if name.strip()}
        packages = [pkg for pkg in packages if pkg["name"] in wanted]

    if not packages:
        print("::error::no packages to check")
        return 2

    print(f"PyPI version check: expecting {expected} ({args.base_url})")
    failures: list[str] = []
    for pkg in packages:
        name = str(pkg["name"])
        actual: str | None = None
        for attempt in range(args.retries + 1):
            try:
                actual = latest_on_pypi(args.base_url, name)
            except RuntimeError as exc:
                print(f"::error::{exc}")
                return 2
            if actual == expected:
                break
            if attempt < args.retries:
                print(
                    f"  {name}: PyPI serves {actual!r}, waiting for {expected} (attempt {attempt + 1}/{args.retries + 1})"
                )
                time.sleep(args.delay)
        status = "OK" if actual == expected else "MISMATCH"
        shown = actual if actual is not None else "not published"
        print(f"  {name:26} PyPI latest={shown:12} expected={expected:12} {status}")
        if actual != expected:
            failures.append(f"{name}: PyPI latest is {shown}, expected {expected}")

    if not failures:
        print("OK: every package is available at the expected version")
        return 0
    for failure in failures:
        print(f"::error::{failure}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
