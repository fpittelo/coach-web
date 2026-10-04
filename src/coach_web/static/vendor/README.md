# Vendored assets — provenance

All frontend assets are self-hosted (zero CDN at runtime). This file records
where each library added by issue #81 was obtained from and its integrity
hashes. `alpine.min.js` and the Inter WOFF2 fonts predate #81 (issues #78/#80).

## marked.min.js — markdown parser

- **Version:** 18.0.14 (UMD build, `lib/marked.umd.js`)
- **Source:** `https://cdn.jsdelivr.net/npm/marked@18.0.14/lib/marked.umd.min.js`
  (official UMD source file minified by jsDelivr/Terser v5.48.0)
- **SHA-256:** `df2d708d65ab3d971218a40bd2f817c0a39a7054f02047cc22fe907a3963e9b6`
- **License:** MIT — see `marked.LICENSE` (© MarkedJS contributors, ©
  Christopher Jeffrey)
- **Global exposed:** `window.marked` (`.parse(text)`)

## DOMPurify.min.js — XSS sanitizer

- **Version:** 3.4.16 (official distribution build, `dist/purify.min.js`)
- **Source:** `https://cdn.jsdelivr.net/npm/dompurify@3.4.16/dist/purify.min.js`
- **SHA-256:** `2c90a9b46d6463f26038a29b686e82bc91de01fdac9d5229e7cfe3b360134ea2`
- **License:** Apache-2.0 **and** MPL-2.0 (dual license, per the upstream
  banner) — see `DOMPurify.LICENSE` (© Cure53 and contributors). Note: the
  upstream npm package ships the Apache-2.0 text as its LICENSE file; the
  dual-license grant is stated in the `/*! @license … */` banner at the top
  of `DOMPurify.min.js` itself.
- **Global exposed:** `window.DOMPurify` (`.sanitize(html)`)

## Upgrade procedure

1. Download the new pinned-version build from the URLs above (bump the
   version segment).
2. Verify the `/*! @license … */` (DOMPurify) or jsDelivr banner (marked)
   states the expected version.
3. Refresh the SHA-256 in this file, keep the license notices, and re-run
   `pytest tests/test_static_assets.py` (the vendor contract tests pin the
   file names and the sanitizer/parser markers).