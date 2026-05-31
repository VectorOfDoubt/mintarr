# ADR-0006: Documentation site tooling — MkDocs Material

**Status:** Accepted — locked 2026-05-26
**Deciders:** Eivind Sjursen, Claude
**Related:** [MINTARR_DOCUMENTATION_INDEX.md §2](../../MINTARR_DOCUMENTATION_INDEX.md)

---

## Context

Mintarr's documentation set is around 30 documents at v1.0 (per [MINTARR_DOCUMENTATION_INDEX.md §4](../../MINTARR_DOCUMENTATION_INDEX.md#4-documentation-layout-target-shape)). Browsing 30 documents through GitHub's Markdown renderer works, but it does not:

- Surface a coherent navigation hierarchy
- Provide full-text search
- Render Mermaid / PlantUML diagrams natively in fenced code blocks
- Show a table of contents per page
- Distinguish "spec" content from "operational" content visually
- Support versioned documentation when Mintarr reaches stable releases

These gaps are not fatal — operators *can* read raw Markdown — but they are a material onboarding friction for the audience Mintarr targets. Self-hosters used to wiki.servarr.com (Sonarr / Lidarr / Radarr documentation) and the Home Assistant docs site expect a real documentation site.

Four serious candidates exist for static-site generation: MkDocs Material, Docusaurus, Hugo (with a Markdown theme like Doks), and Sphinx (with the Furo or Read-the-Docs themes).

## Decision

Mintarr uses **MkDocs Material** as its documentation site generator.

The documentation source lives under `docs/` in the repo. `mkdocs.yml` at the repo root drives the build. GitHub Actions (`.github/workflows/docs.yml`) builds and deploys to GitHub Pages on every push to `main`. The site URL is provisionally `<org>.github.io/mintarr` until a custom domain is decided (deferred — see [MINTARR_DOCUMENTATION_INDEX.md §9](../../MINTARR_DOCUMENTATION_INDEX.md#9-open-questions)).

Documentation versioning (mike plugin) is **not** configured at foundation. Added at first stable release.

## Rationale

### Audience fit

MkDocs Material is the de facto documentation generator for the Python self-hosting ecosystem in 2026. Used by:

- Home Assistant
- Pydantic
- FastAPI
- Plausible
- Glances
- numerous Lidarr-adjacent tools

Mintarr's audience already reads MkDocs Material sites daily. The visual conventions (sidebar layout, search bar in the top right, admonition boxes for warnings/tips, code block copy buttons) are familiar.

### Markdown native

MkDocs Material renders standard Markdown without preprocessor steps. Spec documents, ADRs, design docs, and operational guides written in plain `.md` files render correctly on the site *and* on GitHub's raw view *and* in IDE preview. This three-way compatibility matters because:

- Maintainers read docs in IDE during PR review
- Contributors read docs on GitHub before opening PRs
- Operators read docs on the deployed site

A generator that requires JSX (Docusaurus) or restructured-text (Sphinx) would force one of the three audiences to deal with non-rendered content.

### Mermaid + PlantUML + admonitions out of the box

`PIPELINE.md` and `OVERVIEW.md` include Mermaid component diagrams. MkDocs Material has Mermaid support via a single plugin entry in `mkdocs.yml`. Admonition syntax (`!!! warning`, `!!! note`, `!!! tip`) renders to visually distinct boxes that operational docs lean on heavily.

Hugo and Sphinx can do these but require theme-specific configuration. MkDocs Material does them as defaults.

### Search

Built-in full-text search via lunr.js. No external service required (unlike Algolia DocSearch). No tracking, no third-party SaaS dependency, no API key in the build. Matches Mintarr's general "self-contained" preference.

### CI cost

MkDocs Material builds in seconds for a 30-document site. GitHub Actions free tier is sufficient indefinitely. No build caching gymnastics required.

### Versioned documentation (mike plugin)

When Mintarr reaches v1.0 and starts tagging releases, the `mike` plugin layers version-switching onto MkDocs Material. The documentation URL becomes `<site>/v1.2/strategy/VISION.md`, with a version dropdown. This is added at first stable release; not at foundation.

## Consequences

### Positive

- 30-document site builds and deploys in under a minute
- Search works out of the box; no external SaaS
- Markdown files render correctly in three contexts (site, GitHub, IDE)
- Mermaid diagrams in `PIPELINE.md` and `OVERVIEW.md` render natively
- Visual conventions match audience expectations
- Future versioned documentation is supported by adding the `mike` plugin

### Negative

- `mkdocs.yml` at repo root is one more configuration surface to maintain
- The Material theme's design opinions are baked in (dark/light/auto toggle, specific colour palette options). Customisation requires CSS overrides.
- Plugin ecosystem is large but unevenly maintained; we constrain to first-party plugins (`material`, `mermaid2`, `mike` when needed)

### Accepted trade-offs

- We do not get JSX-style React components. If a doc page needs a live demo, it lives outside the docs site (on the Mintarr container itself).
- Theme customisation is constrained. Mintarr's branding (colour palette, logo) gets a CSS override file under `docs/stylesheets/` rather than a full theme rewrite.

## Alternatives considered

### Alternative 1: Docusaurus (React-based)

Rejected. Heavier toolchain (Node + Yarn / pnpm build pipeline), JSX requires writers to learn React semantics, audience overlap weaker than MkDocs Material. Better for marketing-heavy docs sites (which Mintarr does not need).

### Alternative 2: Hugo (with a Markdown theme)

Considered. Fast builds, no Node dependency, popular in DevOps community. Rejected because: theme ecosystem for technical docs is fragmented, Mermaid support requires theme-specific configuration, audience overlap weaker than MkDocs Material.

### Alternative 3: Sphinx (with Furo or Read-the-Docs theme)

Rejected. reStructuredText vs Markdown is a real cost — we would have to maintain Markdown originals and convert, or write reST and lose GitHub-render compatibility. Sphinx is the right answer for projects with extensive autodoc-extracted Python API documentation; Mintarr does not need that yet.

### Alternative 4: Raw Markdown on GitHub, no static site

Rejected. As described in §Context — works but loses search, navigation, diagram rendering, and audience-familiar conventions. The audience cost is real.

### Alternative 5: Static site at first stable release; raw Markdown during foundation

Considered. Defer the static site until Mintarr has something worth advertising. Rejected because: the foundation documentation set is already at 30 documents and the navigation problem is real *now*, not just at v1.0. Cheaper to set up MkDocs Material once during foundation than to retrofit later.

## Re-evaluation triggers

This ADR is re-opened only if:

1. **MkDocs Material's licensing or maintenance status changes adversely** (project archived, license changes to non-FOSS, theme becomes paid).
2. **Mintarr's documentation needs require features MkDocs Material cannot serve** (e.g., heavy interactive embedded demos, code playgrounds). Tooling can be evaluated against the concrete need.
3. **A successor tool emerges with clearly stronger audience-fit signals.** Unlikely in the 2026-2027 window.

Until then, ADR-0006 stands. New documentation conventions assume MkDocs Material rendering.

## Action items

These follow the ADR's lock and are tracked in the Documentation Index:

- [ ] `mkdocs.yml` at repo root
- [ ] `requirements-docs.txt` with `mkdocs-material`, `pymdown-extensions`, `mkdocs-mermaid2-plugin`
- [ ] `.github/workflows/docs.yml` building and deploying on push to `main`
- [ ] Navigation tree in `mkdocs.yml` mirroring [MINTARR_DOCUMENTATION_INDEX.md §4](../../MINTARR_DOCUMENTATION_INDEX.md#4-documentation-layout-target-shape)
- [ ] CSS overrides for Mintarr branding under `docs/stylesheets/`
- [ ] (Future) `mike` plugin configuration at first stable release

---

> Locked: 2026-05-26
