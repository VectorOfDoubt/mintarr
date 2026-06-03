# ADR-0011: Frontend approach for the Phase 2 dashboard redesign

**Status:** Accepted — locked 2026-06-03
**Deciders:** Eivind Sjursen, Claude
**Related:** [ADR-0008 Strategic positioning](0008-strategic-positioning.md), [ADR-0009 Runtime hardening conventions](0009-runtime-hardening-conventions.md), [ADR-0010 Python implementation language](0010-python-implementation-language.md), [ROADMAP Phase 2](../../strategy/ROADMAP.md), [TESTING.md §2.2](../../development/TESTING.md)

---

## Context

The dashboard is currently server-rendered HTML/CSS/vanilla JS embedded inline in `app/dashboard.py` (2,606 lines; the HTML shell, inline `<style>`, and inline `<script>` all live in `dashboard_page()`). It uses a tab bar (records / integrations), a drawer, periodic API refresh, and `localStorage` for the API key and view state. There is no Node toolchain in the repo: no `package.json`, no Vite/TypeScript/Playwright config, no frontend build step. CI runs ruff, ruff format, mypy, pytest, and a conventional-commits check. Dashboard tests are server-side pytest (`tests/test_dashboard.py`) asserting endpoint shapes and rendered structure.

[ROADMAP Phase 2](../../strategy/ROADMAP.md) — "Operator UI redesign" — is the trigger for this ADR. Its stated goal is that "Mintarr's dashboard feels like an arr-stack tool." Its scope is concrete and already written down:

- Sidebar navigation with seven sections (Overview / Queue / History / Review / Connectors / Settings / System)
- Topbar with global search (jids, artists, albums), notifications, user menu
- Settings: seven card groups (General, Source Connectors, Verifier Connectors, Output Connectors, Quality Policies, Notifications, UI) — each a form with fields, validation, and save/cancel
- System: seven card groups (Status, Workers, Tasks, Logs, Backup, Updates, Events)
- Live worker status (current job per worker, queue depth, restart action)
- Audit log viewer with multi-dimensional filter (level, component, jid) and download
- Dark / light / auto theme switch
- Responsive layout down to a 375px mobile viewport

This ADR is **evaluation only**. It locks a direction so Phase 2 does not re-open the framework debate at kickoff. It does **not** authorise any frontend implementation, dependency vendoring, or `dashboard.py` refactor — those are separate Phase 2 work items.

A repo-based technical assessment by Codex ([issue #14](https://github.com/eivindsjursen-lab/mintarr/issues/14)) recommended either "no framework yet" or "HTMX + Alpine.js as the preferred incremental path … only if ADR-0011 needs to choose a concrete future direction now," and explicitly asked for the product/UX side to be challenged before accepting it. This ADR does that challenge and reaches a firmer position than the hedge.

## Decision

**Mintarr's Phase 2 dashboard redesign will use HTMX + Alpine.js over server-rendered Flask, with both libraries vendored as pinned, SRI-pinned static assets. No Node toolchain is introduced. No SPA framework (React, Vue, Svelte/SvelteKit) is adopted.**

This is a committed direction, not an "escape hatch we might reach for." The choice is locked now so the Phase 2 milestone starts with a decided rendering model instead of re-litigating it.

Two guardrails are part of the decision:

1. **Extraction is the first step and is framework-agnostic.** Before HTMX/Alpine are introduced, the inline HTML/CSS/JS in `dashboard.py` is extracted into Flask templates (`templates/`) and static assets (`static/`). This refactor delivers value on its own (a 2,606-line module shrinks, rendering becomes testable per-partial) and is a precondition for HTMX swaps and vendored Alpine regardless of which library wins. If for any reason the rest of this decision were revisited, the extraction is not wasted.
2. **Flask stays the source of truth.** Rendering stays server-side and Python-owned, consistent with [ADR-0010](0010-python-implementation-language.md). HTMX returns server-rendered HTML partials; Alpine handles local UI state only. The dashboard does not become a client-owned application with the server reduced to a JSON API.

## Rationale

### The product/UX challenge to "no framework yet"

Codex's assessment is repo-centric — build chain, CI lanes, dependency surface — and is correct on every one of those points. Where it under-weights the decision is the product: **Phase 2 is not "small dashboard iterations." It is a ground-up operator console.** Counting the roadmap scope, it is roughly fourteen card surfaces, a seven-section navigation shell, global search, live worker data, a filtered audit-log table, a theme system, and a responsive mobile layout. That is materially more interactive state than the current two-tab UI.

"No framework yet, revisit when Phase 2 starts implementation" defers the decision rather than making it. The roadmap requirements are already concrete and committed — we are not waiting on missing requirements to choose. Deferring **guarantees** we re-have this exact conversation at Phase 2 kickoff, which is the opposite of what an ADR is for. [ADR-0010](0010-python-implementation-language.md) locked the language specifically to stop expensive conversations from recurring; the same discipline applies here. The issue's "Done when" asks for a locked choice, and "undecided, maybe later" is the weakest possible lock.

### Why HTMX + Alpine specifically, and why the "escape hatch" framing is backwards

The Phase 2 surfaces split cleanly into two kinds of interactivity, and HTMX and Alpine map onto them:

- **Server-state surfaces** — Queue, History, Review, Connectors, live worker status, audit-log table. These are server-owned data rendered as lists/tables that refresh and act (cancel, promote, discard, retry, restart). This is exactly HTMX's model: an attribute requests a partial and swaps it into the DOM. The server keeps owning the truth.
- **Local-state surfaces** — sidebar collapse, mobile drawer, theme toggle, form dirty-state, validation display, modal/drawer open state. This is exactly Alpine's model: small islands of declarative local state with no build step.

Phase 2 needs **both** kinds simultaneously. That is the core argument against the "vanilla JS until it hurts, then reach for a tool" framing: the pain is **structurally predictable from the roadmap**, not something to discover empirically. The Settings section alone is seven multi-field card forms; hand-rolling fetch → serialise → validate → render → error for each in vanilla JS is precisely the boilerplate Alpine removes. Deferring means writing Phase 2 vanilla JS that we already know we will want to rewrite.

### Maintainer and AI-assistance fit

[ADR-0010](0010-python-implementation-language.md) records that Python-primary maintenance and reliable AI-assisted contribution are load-bearing. HTMX keeps the logic in Python — the backend returns HTML — so most of Phase 2's interactivity is authored in the language the maintainer and the AI assistants are strongest in. Alpine is small, declarative, and inline; it does not require learning a component/build/state-management toolchain. This fits the team better than any JS-heavy framework.

### Testing cost is the deciding practical factor

[TESTING.md §2.2](../../development/TESTING.md) notes that whichever framework wins drives the test stack, and that an SPA framework pulls in the full Playwright + Vitest/Testing-Library + Storybook pyramid. HTMX + Alpine avoids almost all of that:

- Because rendering stays server-side, the **existing pytest endpoint tests keep asserting the rendered HTML partials.** We get real coverage of HTMX swaps with the test layer we already run, in ~3 seconds, no browser.
- Playwright becomes a **targeted** addition for the few genuinely client-stateful behaviours (theme persistence, sidebar collapse, dirty-form guards), not a wholesale new pyramid.
- CI is unchanged: ruff / format / mypy / pytest still cover all the Python, which remains where the logic lives.

An SPA framework would invert this — most logic moves client-side, out of pytest's reach, forcing the full browser/component test investment before the product has earned it.

### Zero Node toolchain

HTMX (~14 KB) and Alpine (~15 KB) are vendored as pinned static files with subresource-integrity hashes. No npm, no Vite, no `package.json`, no bundler, no frontend CI lane, no deployment change. The repo's "no Node surface" property — which Codex correctly identified as the reason SPA frameworks are premature — is preserved exactly. This is the decisive separation between HTMX/Alpine and every SPA option: the former costs two static files, the latter costs a toolchain.

### Positioning consistency

[ADR-0008](0008-strategic-positioning.md) frames Mintarr as a backend-heavy, operator-focused arr-stack companion. Arr-stack admin UIs are server-authoritative config-and-status panels, not rich client applications. A server-rendered HTMX/Alpine dashboard matches that shape. An SPA would impose client-side architecture the product does not need.

## Consequences

### Positive

- Phase 2 starts with a decided rendering model; the framework debate does not recur at kickoff.
- No Node toolchain, no build step, no new CI lane. Existing ruff/format/mypy/pytest coverage stays relevant and load-bearing.
- The template/static extraction (guardrail 1) shrinks `dashboard.py` and makes rendering testable per-partial — a maintainability win independent of the library choice.
- Most Phase 2 interactivity is authored in Python (HTMX) — the maintainer- and AI-strongest path.
- Playwright is added narrowly and only when a genuinely client-stateful surface needs it, per [TESTING.md §2.2](../../development/TESTING.md), rather than as a precondition.

### Negative

- HTMX and Alpine are still new concepts and vendored dependencies the team must learn and keep patched (mitigated: small surface, pinned + SRI, no transitive npm tree).
- Alpine local state can fragment if it creeps into surfaces that should stay server-owned. Phase 2 needs a written convention: HTMX for server state, Alpine for local UI state only.
- Two libraries instead of zero is a real, if small, increase over staying on pure vanilla JS.
- A vendored-asset policy (pin version, store the SRI hash, document the upgrade procedure) must be authored as part of Phase 2 — it does not exist today.

### Accepted trade-offs

- We choose a direction now on the strength of an already-concrete roadmap, accepting that Phase 2 could be rescoped. If it is rescoped *smaller*, the cost of having chosen is two unused static files; if rescoped *larger*, HTMX/Alpine still fit. The asymmetry favours deciding now.
- We forgo the richest client-side component ergonomics (SPA frameworks) in exchange for keeping logic in Python and tests in pytest. For an operator console this is the right trade.

## Alternatives considered

### Alternative 1: No framework yet — keep vanilla JS, revisit at Phase 2 (Codex's first option)

Rejected as the *decision*, though its caution is respected in guardrail 1. It maximises optionality but defers rather than decides, and it guarantees re-litigating the choice at Phase 2 kickoff against a roadmap that is already concrete. Vanilla JS for seven Settings forms plus live tables is known-painful in advance; "wait until it hurts" means knowingly writing throwaway code. The extraction work this option would still need is folded into our guardrail, so its one durable benefit is captured without its indecision.

### Alternative 2: Svelte / SvelteKit

Rejected for now. Pleasant component model, but it requires Node, bundling, a frontend CI lane, Vitest/Playwright, a static-asset/serving strategy, and a deployment change — the whole toolchain Codex correctly flags as premature. SvelteKit specifically duplicates the app-server role Flask already holds ([ADR-0010](0010-python-implementation-language.md)). The migration cliff is far larger than HTMX/Alpine for no product capability Phase 2 actually requires.

### Alternative 3: Vue 3

Rejected for now. Same Node/build/test overhead as Svelte for this repo. Used progressively inside one Flask-rendered page it risks a hybrid that is neither cleanly server-rendered nor cleanly componentised — the worst of both. No Phase 2 surface needs Vue's reactivity ceiling.

### Alternative 4: React

Rejected. Heaviest option, largest dependency and architectural surface, most likely to pull Mintarr into SPA architecture before the product needs it. Wrong fit for a backend-heavy operator tool ([ADR-0008](0008-strategic-positioning.md)). Community familiarity does not offset the cost here.

### Alternative 5: HTMX or Alpine alone

Rejected. HTMX alone cannot cleanly express purely local UI state (theme toggle, sidebar collapse, dirty-form tracking) without falling back to ad-hoc vanilla JS — reintroducing the boilerplate we are trying to remove. Alpine alone cannot express server-authoritative partial swaps without hand-rolled fetch/render. Phase 2 needs both models; the two libraries are complementary, not redundant.

## Re-evaluation triggers

This ADR is re-opened only if one of the following holds. Each is measured at Phase 2 implementation, not assumed in advance:

1. **A surface needs interaction that server round-trips structurally cannot serve at acceptable latency** — e.g. complex drag-and-drop reordering, real-time collaborative editing, or an offline-capable PWA. None of these is in the current Phase 2 scope.
2. **Alpine local state for a single surface exceeds a complexity threshold** — deeply nested reactive state or cross-component shared stores — where a real reactive framework would be genuinely simpler. This is judged on built code, not predicted.
3. **A Node toolchain is added to the repo for another reason** (a TypeScript SDK, a docs pipeline that needs it), removing the "no Node" cost that currently rules out SPA frameworks. At that point Svelte/Vue stop being toolchain-prohibitive and a successor ADR may compare them on merits.
4. **Operators demand a UX that server-rendered partials cannot deliver** (native-app feel, offline use), reported in practice rather than anticipated.
5. **HTMX or Alpine becomes unmaintained or a security liability** with no comparable vendorable successor.

Until one of these is met, ADR-0011 stands. Proposals to adopt an SPA framework for Phase 2 are closed with a reference to this ADR and an invitation to propose a successor ADR addressing the triggers above.

## Out of scope for this ADR

- **Implementation.** No templates, static assets, vendored libraries, or `dashboard.py` refactor are authored under this ADR. Those are Phase 2 work items gated on this decision.
- **The vendored-asset policy details** (exact pinned versions, SRI hashes, upgrade procedure). Authored when Phase 2 implementation begins.
- **Backend API shape changes.** Whether HTMX endpoints return partials from existing or new routes is a Phase 2 design question.
- **The Phase 2 design system** (component inventory, spacing/colour tokens, theme variables). A design concern, not a framework decision.

---

> Locked: 2026-06-03
