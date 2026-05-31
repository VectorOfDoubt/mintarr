# ADR-0005: License — AGPL-3.0-only

**Status:** Accepted — locked 2026-05-31 (Alternative A from prior draft)
**Deciders:** Eivind Sjursen, Claude, Codex
**Related:** [ADR-0001 Rename](0001-rename-from-tidalhires.md)

---

## Decision

**Mintarr is licensed under the GNU Affero General Public License, version 3 only (AGPL-3.0-only).**

The combined work distributed in Mintarr's container image includes the AGPL-3.0-only `tidal-dl-ng-For-DJ` fork. AGPL-3.0 is the strictest copyleft license present in the combined work; the combined work is therefore AGPL-3.0-only.

The `LICENSE` file at the repository root contains the verbatim AGPL-3.0-only text. The README, documentation index, contributing guide, MkDocs footer, and SECURITY.md all reference AGPL-3.0.

Operators redistributing Mintarr — including hosting Mintarr as a service that users interact with over a network — must offer the corresponding source code to those users on request, per the AGPL-3.0 §13 "Remote Network Interaction" clause.

This decision supersedes the three pending alternatives recorded in the original draft of this ADR. Those alternatives are kept below for historical context but are no longer in play.

---

---

## Context

Foundation decision §1 in [MINTARR_DOCUMENTATION_INDEX.md](../../MINTARR_DOCUMENTATION_INDEX.md) tentatively locked GPL-3.0-only as the Mintarr license, matching the arr-stack family (Lidarr / Sonarr / Radarr / Prowlarr are GPL-3.0-only).

Codex's publication audit raised a license-compatibility blocker that invalidates the simple "GPL-3.0-only" choice. Specifically:

- The TIDAL source adapter installs and invokes Radexito's `tidal-dl-ng-For-DJ` fork (pinned commit `87ec210dfeeef23441b7c99a16123a25ec63f207`)
- That fork is licensed **AGPL-3.0-only**
- AGPL-3.0 is asymmetrically compatible with GPL-3.0: combining AGPL code with GPL code produces an AGPL combined work, not a GPL combined work
- Distributing Mintarr (as a container image, as PyPI package, as source) with the AGPL dependency vendored or pinned would mean the combined work must be licensed AGPL-3.0-only

This means Mintarr cannot be straightforwardly GPL-3.0-only while shipping a pinned AGPL-3.0 dependency.

Three resolution paths exist, listed below. Each has consequences that ripple into [README.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/README.md), [CONTRIBUTING.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/CONTRIBUTING.md), and operator documentation.

This ADR records the three options and the maintainer's recommendation; Eivind chooses the final answer.

## Decision (pending)

Choose one of the three alternatives below. Once locked, this ADR is amended with the chosen answer and re-categorised as `Accepted`.

### Alternative A — License Mintarr as **AGPL-3.0-only**

Mintarr inherits the strictest of its components' licenses. Combined work is AGPL-3.0; distribution and SaaS-style hosting both trigger copyleft obligations.

**Implications:**
- LICENSE file is the AGPL-3.0-only text
- Container images, source distributions, and PyPI packages all carry AGPL-3.0 obligations
- Operators hosting Mintarr as a service (e.g., behind a proxy serving other users) must offer source to those users on request
- Compatible with Lidarr's GPL-3.0 dependency direction at runtime (AGPL software calling GPL software is fine; the question is the other direction)
- Closes the door on combining Mintarr with future GPL-2.0-only dependencies

**When this is right:** if Mintarr is unambiguously a self-hostable application that users run for themselves, and we accept that SaaS deployments must publish source.

### Alternative B — License Mintarr as **GPL-3.0-only**, treat tidal-dl-ng-For-DJ as an external runtime dependency

Make the TIDAL adapter pull `tidal-dl-ng-For-DJ` at container build time as a separate external tool, not vendored Mintarr code. Document that operators using the TIDAL adapter pull AGPL code into their image and inherit AGPL obligations for that subsystem only.

**Implications:**
- LICENSE file is GPL-3.0-only
- Operators *not* using TIDAL ship pure GPL-3.0 Mintarr
- Operators *using* TIDAL build a combined work that is AGPL-3.0; documented as their responsibility
- The TIDAL adapter is technically optional even though it is the original use case
- Significant complexity for operators: "your effective license depends on which adapters you enable"
- May be legally borderline if Mintarr's default Dockerfile installs the dependency unconditionally

**When this is right:** if we want to ship GPL-3.0 Mintarr by default and shift the AGPL question to a TIDAL-specific opt-in. Requires careful documentation and an `optional-tidal` compose profile pattern.

### Alternative C — Replace `tidal-dl-ng-For-DJ` with a GPL-compatible alternative

Find or build a TIDAL fetch path that does not pull an AGPL dependency. Options to investigate:

- Direct `tidalapi` usage (BSD-3-Clause — GPL-compatible) for both search and download
- An MIT-licensed TIDAL client (if one exists with HiRes support)
- Write a minimal Mintarr-owned TIDAL fetcher

**Implications:**
- LICENSE file is GPL-3.0-only
- TIDAL adapter is rewritten to remove the `tidal-dl-ng-For-DJ` dependency
- Significant engineering work (estimated 20-40 hours depending on whether existing GPL-compatible options handle HiRes)
- Cleanest legal position
- Removes Radexito-fork-specific quirks and bug fixes we currently inherit

**When this is right:** if Mintarr's license clarity is worth the engineering investment to remove the AGPL dependency. Strongest long-term position; highest upfront cost.

## Recommendation (Claude)

**Alternative A** is the lowest-risk path forward.

Reasoning:

- AGPL-3.0 is the de facto license of self-hosted-with-community-PRs projects in 2026 (Plausible, Mastodon, Grafana ≤8.x, Bitwarden, Nextcloud all AGPL or AGPL-derived)
- Mintarr's audience is comfortable with AGPL — they already run AGPL applications
- Alternative B has legal ambiguity and operator-confusion costs that outweigh its theoretical purity
- Alternative C is right *eventually* but blocks Phase 0 cutover for 1-2 weeks of engineering work that does not produce user-visible value

Alternative A locks license immediately, unblocks LICENSE file creation, and lets Phase 0 cutover proceed. Alternative C can be pursued as a separate, scheduled task once Mintarr is public and stable.

## Rationale framework (for whichever is chosen)

Whichever alternative is chosen, the rationale that goes into the locked ADR addresses these questions:

1. **What is Mintarr's license, and why?**
2. **What is the AGPL dependency, and how is it handled?**
3. **What obligations do operators inherit?**
4. **How is the license communicated to contributors?**
5. **What triggers a re-evaluation?**

## Consequences (depend on choice)

| Consequence | A: AGPL | B: GPL + opt-in | C: GPL after dep replacement |
|---|---|---|---|
| LICENSE file | AGPL-3.0-only | GPL-3.0-only | GPL-3.0-only |
| Operator SaaS source obligations | Yes | Conditional (TIDAL only) | No |
| Mintarr ↔ Lidarr ecosystem clarity | Slight divergence (Lidarr is GPL) | Clear match | Clear match |
| Cutover timeline impact | None | ~1 day docs work | 20-40h engineering |
| Long-term legal position | Stable but stricter | Borderline / operator-burden | Cleanest |
| Community-friendliness | Matches self-hosting norms | Confusing license matrix | Maximally permissive within copyleft |

## Re-evaluation triggers (independent of which alternative is chosen)

This ADR's locked version is re-opened only if:

1. **The AGPL dependency's license changes upstream** (e.g., Radexito relicenses `tidal-dl-ng-For-DJ` under GPL-3.0). Would unlock simpler license positions.
2. **A material new dependency is added with an incompatible license.** Forces re-evaluation in either direction.
3. **A SaaS-hosting business model becomes a Mintarr goal.** May favour Alternative A over C if not already there.

## Action items completed at lock (2026-05-31)

- [x] Eivind delegated final choice to Claude; Alternative A (AGPL-3.0-only) selected per the recommendation above
- [x] `LICENSE` file added to repo root with verbatim AGPL-3.0-only text
- [x] `MINTARR_DOCUMENTATION_INDEX.md` foundation-decisions table updated
- [x] `README.md` license badge updated to AGPL-3.0
- [x] `CONTRIBUTING.md` license expectations updated
- [x] `mintarr/mkdocs.yml` footer updated
- [x] `SECURITY.md` mentions AGPL-3.0 operator obligations
- [x] `CUTOVER_MANIFEST.md` license-blocker entries marked resolved

Alternative C (replace `tidal-dl-ng-For-DJ` with a GPL-compatible alternative) remains an option for future Mintarr versions if the AGPL dependency becomes inconvenient. Pursuing it would require its own design doc and a successor ADR; AGPL-3.0 holds until then.

## Re-evaluation triggers

This ADR is re-opened only if:

1. **The `tidal-dl-ng-For-DJ` dependency's license changes upstream** (e.g., Radexito relicenses under GPL-3.0). Could unblock GPL-3.0-only.
2. **A material new dependency is added with a more restrictive copyleft.** Combined work license must escalate.
3. **A SaaS-hosting business plan emerges and AGPL §13 operator obligations become a friction point for adopters.** Unlikely but possible.
4. **Alternative C lands** (TIDAL fetcher rewritten to remove the AGPL dependency). Successor ADR can downgrade to GPL-3.0-only.

Until then, ADR-0005 stands: Mintarr is AGPL-3.0-only.

---

> Locked: 2026-05-31
