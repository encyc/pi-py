"""The bundled engine assets must stay byte-identical to what was validated."""

from __future__ import annotations

from pi_codemode import PRELUDE_SHA256, WASM_SHA256, WASM_SIZE, load_prelude, load_wasm
from pi_codemode.assets import PRELUDE_CHARS, asset_dir, prelude_sha256, wasm_sha256

#: quickjs-ng version reported by the bundled wasm (Phase-0 S1).
EXPECTED_QUICKJS_VERSION = "0.17.0"


def test_wasm_asset_is_the_pinned_engine() -> None:
    data = load_wasm()
    assert len(data) == WASM_SIZE == 637_405
    assert wasm_sha256(data) == WASM_SHA256
    # vercel-labs/quickjs-wasi 3.6.2, the exact binary upstream pins in
    # packages/codemode/package.json:70.
    assert WASM_SHA256 == "d4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9"


def test_prelude_asset_is_the_upstream_source_verbatim() -> None:
    prelude = load_prelude()
    assert len(prelude) == PRELUDE_CHARS == 17_233
    assert prelude.count("\n") + 1 == 475
    assert prelude_sha256(prelude) == PRELUDE_SHA256
    assert PRELUDE_SHA256 == "57c8dc8c0e812117d82167fa751bc368f249d5aaee56d321066fc5280c785281"
    # Shape of upstream PRELUDE_SOURCE: an IIFE taking (bridge, toolsJson, globalsJson, storeJson).
    assert prelude.startswith("(function (bridge, toolsJson, globalsJson, storeJson) {")
    assert prelude.endswith("})")
    assert "return {\n\t\tsettle(id, ok, payload, propertiesJson) {" in prelude
    # Template interpolations were evaluated when the asset was produced.
    assert "${" not in prelude
    for constant in ("16777216", "100000", "262144", "1048576"):
        assert constant in prelude


def test_assets_live_inside_the_package() -> None:
    directory = asset_dir()
    assert (directory / "quickjs.wasm").is_file()
    assert (directory / "prelude.js").is_file()
    assert directory.parent.name == "pi_codemode"
