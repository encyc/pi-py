"""Bundled engine assets: ``quickjs.wasm`` + the upstream ``PRELUDE_SOURCE``.

Both files are shipped inside the package (``pi_codemode/_assets/``) and their
digests are asserted by ``tests/test_assets.py``, so a silent engine or prelude
upgrade fails CI instead of changing sandbox behaviour quietly.

* ``quickjs.wasm`` — `vercel-labs/quickjs-wasi`_ **3.6.2** (MIT), the exact binary
  `@earendil-works/pi-codemode` 1.3.0 pins (``packages/codemode/package.json:70``).
  Upstream calls ``loadQuickJSWasm()`` on this file; we drive the same C ABI from
  Python through ``wasmtime``.
* ``prelude.js`` — the *evaluated* value of ``PRELUDE_SOURCE`` from
  ``packages/codemode/src/runtime/prelude-source.ts`` at v1.3.0, byte for byte.
  It is JavaScript evaluated inside the VM before the script runs; it is not edited
  here (see ``PORTING.md`` for how the file is produced and re-verified).

.. _vercel-labs/quickjs-wasi: https://github.com/vercel-labs/quickjs-wasi
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

__all__ = [
    "ASSETS_DIR",
    "WASM_PACKAGE_VERSION",
    "PRELUDE_CHARS",
    "PRELUDE_SHA256",
    "WASM_SHA256",
    "WASM_SIZE",
    "WasmIntegrityError",
    "asset_dir",
    "load_prelude",
    "load_wasm",
    "prelude_sha256",
    "wasm_sha256",
]

#: Digest of ``_assets/quickjs.wasm`` (vercel-labs/quickjs-wasi 3.6.2).
WASM_PACKAGE_VERSION = "3.6.2"
WASM_SHA256 = "d4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9"
#: Size in bytes of ``_assets/quickjs.wasm``.
WASM_SIZE = 637_405
#: Digest of the evaluated upstream ``PRELUDE_SOURCE`` (17,233 characters).
PRELUDE_SHA256 = "57c8dc8c0e812117d82167fa751bc368f249d5aaee56d321066fc5280c785281"
#: Character count of the prelude (upstream: 17,233).
PRELUDE_CHARS = 17_233

ASSETS_DIR = Path(__file__).resolve().parent / "_assets"
WASM_PATH = ASSETS_DIR / "quickjs.wasm"
PRELUDE_PATH = ASSETS_DIR / "prelude.js"


class WasmIntegrityError(RuntimeError):
    """A bundled asset does not match the digest recorded at port time."""


def asset_dir() -> Path:
    """Directory holding the bundled ``quickjs.wasm`` / ``prelude.js``."""
    return ASSETS_DIR


def wasm_sha256(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def prelude_sha256(text: str) -> str:
    """Return the lowercase hex SHA-256 of the UTF-8 encoded ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def load_wasm() -> bytes:
    """Read the bundled ``quickjs.wasm``, verifying its digest.

    The bytes are cached for the process lifetime: the module is ~637 KiB and is
    compiled once per :class:`~pi_codemode.host.QuickJSEngine`.
    """
    data = WASM_PATH.read_bytes()
    digest = wasm_sha256(data)
    if digest != WASM_SHA256:
        raise WasmIntegrityError(
            f"quickjs.wasm digest mismatch: expected {WASM_SHA256}, got {digest}. "
            "The engine binary changed; re-run the Phase-0 checks before trusting it."
        )
    return data


@lru_cache(maxsize=1)
def load_prelude() -> str:
    """Return the upstream ``PRELUDE_SOURCE`` exactly as it is evaluated in the VM."""
    text = PRELUDE_PATH.read_text(encoding="utf-8")
    digest = prelude_sha256(text)
    if digest != PRELUDE_SHA256:
        raise WasmIntegrityError(
            f"prelude.js digest mismatch: expected {PRELUDE_SHA256}, got {digest}. "
            "The prelude must stay byte-identical to upstream's evaluated string."
        )
    return text
