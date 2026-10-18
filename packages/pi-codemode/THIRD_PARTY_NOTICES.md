# Third-party notices — bundled engine assets

`src/pi_codemode/_assets/quickjs.wasm` and `src/pi_codemode/_assets/prelude.js` are
bundled verbatim. They are not covered by this repository's MIT license; the notices
below apply.

## `quickjs.wasm` — vercel-labs/quickjs-wasi 3.6.2

- Source: <https://registry.npmjs.org/quickjs-wasi/-/quickjs-wasi-3.6.2.tgz>
  (package `quickjs-wasi@3.6.2`, repository <https://github.com/vercel-labs/quickjs-wasi>)
- License: **MIT** — Copyright (c) 2026 Vercel, Inc.
- Size: 637,405 bytes
- SHA-256: `d4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9`
- Contains **QuickJS-NG** (`qjs_get_quickjs_version()` reports `0.17.0`) compiled to
  `wasm32-wasip1`; QuickJS-NG is MIT licensed:
  <https://github.com/quickjs-ng/quickjs>

The upstream wrapper's LICENSE file is distributed inside the npm package; the bundled
QuickJS-NG engine retains its own copyright and license notice.

## `prelude.js` — `@earendil-works/pi-codemode` 1.3.0

- Source: <https://github.com/earendil-works/pi> →
  `packages/codemode/src/runtime/prelude-source.ts`
- License: **MIT** (the `@earendil-works/pi-codemode` package declares MIT)
- This file is the *evaluated* value of the `PRELUDE_SOURCE` template literal at v1.3.0,
  17,233 characters, byte-identical to what upstream evaluates inside the VM.

## MIT license text

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
