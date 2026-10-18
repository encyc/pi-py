#!/usr/bin/env python3
"""Download ``quickjs.wasm`` from npm and refresh the bundled engine asset.

Usage::

    python packages/pi-codemode/scripts/fetch_quickjs_wasm.py 3.6.2
    python packages/pi-codemode/scripts/fetch_quickjs_wasm.py 3.6.2 --write

Without ``--write`` the script only reports what it would do (and the new digest), so an
engine upgrade is always an explicit, reviewable change:

1. run with ``--write`` to replace ``src/pi_codemode/_assets/quickjs.wasm``;
2. update ``WASM_SHA256`` / ``WASM_SIZE`` in ``pi_codemode/assets.py``;
3. ``uv run pytest packages/pi-codemode`` — the Phase-0 regressions must stay green.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "src" / "pi_codemode" / "_assets"
TARBALL = "https://registry.npmjs.org/quickjs-wasi/-/quickjs-wasi-{version}.tgz"
MEMBER = "package/quickjs.wasm"


def fetch(version: str) -> bytes:
    url = TARBALL.format(version=version)
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed https URL
        payload = response.read()
    print(f"  tarball: {len(payload)} bytes sha256={hashlib.sha256(payload).hexdigest()}")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        extracted = archive.extractfile(MEMBER)
        if extracted is None:
            raise SystemExit(f"{MEMBER} not found in {url}")
        return extracted.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="quickjs-wasi version, e.g. 3.6.2")
    parser.add_argument("--write", action="store_true", help="overwrite the bundled asset")
    args = parser.parse_args()

    data = fetch(args.version)
    digest = hashlib.sha256(data).hexdigest()
    print(f"quickjs.wasm: {len(data)} bytes sha256={digest}")
    print(f"current asset: {ASSETS / 'quickjs.wasm'}")

    if not args.write:
        print("dry run; pass --write to replace the asset, then update WASM_SHA256/WASM_SIZE")
        return 0

    (ASSETS / "quickjs.wasm").write_bytes(data)
    print("written. Update pi_codemode/assets.py:")
    print(f'  WASM_SHA256 = "{digest}"')
    print(f"  WASM_SIZE = {len(data)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
