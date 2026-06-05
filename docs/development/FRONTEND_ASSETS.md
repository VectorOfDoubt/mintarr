# Frontend vendored assets

> **Type:** Development / frontend
> **Status:** Living document. Updated when a vendored asset is added or bumped.
> **Audience:** Anyone adding or upgrading a client-side library in the dashboard.

---

## 1. Policy

[ADR-0011](../architecture/adr/0011-frontend-framework.md) commits Mintarr to a
server-rendered Flask dashboard progressively enhanced with **Alpine.js** (local
UI state) and later **HTMX** (server-rendered partial swaps). To keep the
**no-Node** property, these libraries are not installed via npm or built — they
are **vendored as pinned, self-hosted static files** under `app/static/vendor/`
and served from Mintarr's own origin.

Rules for every vendored asset:

1. **Pin an exact version** in the filename (e.g. `alpine-3.14.1.min.js`). Never
   a floating `latest`.
2. **Self-host.** No runtime CDN dependency — the file lives in the repo and
   ships in the image via `COPY app/ /app/`.
3. **Record an SRI hash** (`sha384`) here and set it as the `integrity`
   attribute on the `<script>` tag, so a corrupted or swapped file fails closed.
4. **One library per real need.** Alpine is vendored because the theme switch
   (and future local-state UI) needs it. HTMX is **not** vendored yet — it is
   added only when the first server-partial surface lands, per ADR-0011.

## 2. Currently vendored

| Asset | Version | File | SRI (`sha384`) |
|---|---|---|---|
| Alpine.js | 3.14.1 | `app/static/vendor/alpine-3.14.1.min.js` | `l8f0VcPi/M1iHPv8egOnY/15TDwqgbOR1anMIJWvU6nLRgZVLTLSaNqi/TOoT5Fh` |
| HTMX | 2.0.3 | `app/static/vendor/htmx-2.0.3.min.js` | `0895/pl2MU10Hqc6jd4RvrthNlDiE9U1tWmX7WRESftEDRosgxNsQG/Ze9YMRzHq` |
| Swagger UI (CSS) | 5.17.14 | `app/static/vendor/swagger-ui-5.17.14.css` | `wxLW6kwyHktdDGr6Pv1zgm/VGJh99lfUbzSn6HNHBENZlCN7W602k9VkGdxuFvPn` |
| Swagger UI (JS bundle) | 5.17.14 | `app/static/vendor/swagger-ui-bundle-5.17.14.js` | `wmyclcVGX/WhUkdkATwhaK1X1JtiNrr2EoYJ+diV3vj4v6OC5yCeSu+yW13SYJep` |

HTMX was added in Phase 2 slice 3 for the live Queue partial (server-rendered
fragment polled via `hx-trigger`). HTMX requests authenticate via an
`htmx:configRequest` hook in `dashboard.js` that adds the stored `X-Api-Key`.

## 3. Adding or upgrading an asset

```bash
# 1. Fetch the exact pinned version from a reputable mirror.
curl -fsSL "https://unpkg.com/alpinejs@3.14.1/dist/cdn.min.js" \
  -o app/static/vendor/alpine-3.14.1.min.js

# 2. Compute the SRI hash.
echo "sha384-$(openssl dgst -sha384 -binary app/static/vendor/alpine-3.14.1.min.js \
  | openssl base64 -A)"

# 3. Update the <script integrity="..."> tag in the template AND the table above.
# 4. When upgrading, delete the old pinned file and update every reference.
```

A bump is a normal PR: pin the new version, refresh the SRI, update the
template reference and this table, and confirm the dashboard still renders.

## 4. Why no build step

A bundler/minifier/Node toolchain is explicitly out of scope until ADR-0011's
re-evaluation triggers fire (see that ADR). Vendored minified files give us
pinned, hashed, offline-capable assets with none of the npm/Vite/CI surface.
