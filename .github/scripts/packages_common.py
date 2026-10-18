"""Shared helpers for the release checks in ``.github/scripts/``.

Only stdlib is used so the scripts run on a bare GitHub Actions runner
(``python3`` is preinstalled) without ``uv sync``.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

SEMVER_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def find_repo_root(start: Path | None = None) -> Path:
    """Return the repository root (the directory that contains ``.git``)."""
    anchor = (start or Path(__file__)).resolve()
    candidates = [anchor] if anchor.is_dir() else [anchor.parent]
    for parent in candidates[0].parents:
        if (parent / ".git").exists():
            return parent
    # Fallback for exotic CI layouts: this file lives at <root>/.github/scripts/.
    return Path(__file__).resolve().parents[2]


def discover_packages(root: Path) -> list[dict[str, object]]:
    """Read every workspace package under ``<root>/packages/*/pyproject.toml``.

    Returns a list of dicts with ``name``, ``version``, ``private`` and ``path``.
    The repository root package is intentionally excluded: only members of the
    ``packages/`` workspace are published to PyPI.
    """
    packages: list[dict[str, object]] = []
    for toml_path in sorted((root / "packages").glob("*/pyproject.toml")):
        with toml_path.open("rb") as fh:
            data = tomllib.load(fh)
        project = data.get("project") or {}
        name = project.get("name")
        version = project.get("version")
        if not name or not version:
            continue
        packages.append(
            {
                "name": str(name),
                "version": str(version),
                "private": bool(project.get("private", False)),
                "path": toml_path,
            }
        )
    return packages


def run_git(root: Path, *args: str) -> str:
    """Run a git command inside ``root`` and return its stripped stdout."""
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()
