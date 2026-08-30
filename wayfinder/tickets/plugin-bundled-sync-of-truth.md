# Decide: bundled-plugin single source of truth

wayfinder:grilling

## Question

First-party plugin code now lives in two places: `bundled_plugins/` baked
into the fnack image (Dockerfile `COPY bundled_plugins /app/bundled_plugins`),
and `tajanthind/fnack-plugins/plugins/` packaged by `package_plugins.py` into
versioned release zips + `index.json`. If these drift independently, two
implementations of `fnack.spotiflac` etc. silently diverge. Which is the
source of truth, and what is the release process that keeps the other honest?

## Resolution

**`tajanthind/fnack-plugins` is the source of truth** for first-party plugin
code — it is the marketplace catalogue (`index.json`, `dist/`,
`package_plugins.py`) that both the Marketplace tab and repo installs consume.

Release process (documented standing decision):
1. Edit plugin sources in `fnack-plugins/plugins/<id>/`.
2. Run `python3 package_plugins.py` in `fnack-plugins` to regenerate
   `dist/<id>.zip` + `index.json` (sha256 + download_urls).
3. Copy the same `<id>/plugin.json` + `<id>/plugin.py` into fnack's
   `bundled_plugins/<id>/` so the image-baked copy stays byte-identical.
4. Tag a fnack core release (v0.3.x) — bundled plugins ship in the image so
   fresh installs work with zero marketplace visits; the fnack-plugins repo
   is what Updates + Marketplace re-installs resolve against.

Verified at reconciliation time: all 17 bundled plugins are byte-identical to
their `fnack-plugins/plugins/` counterparts (deep diff, no differences).

Reconciliation ticket for Brief 3 §4.
