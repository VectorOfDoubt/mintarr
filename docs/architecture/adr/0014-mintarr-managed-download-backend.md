# ADR-0014: Mintarr-managed SAB/qBit backend lane — scoped successor to the deferred universal gate

**Status:** Accepted — locked 2026-06-13
**Deciders:** Eivind Sjursen, Claude
**Related:** [ADR-0012 QC import-gate scope](0012-qc-import-gate-scope.md), [ADR-0008 Strategic positioning](0008-strategic-positioning.md), [ADR-0007 No Lidarr fork](0007-no-lidarr-fork.md), [Mintarr-managed SAB/qBit download-client lane](../../design/MINTARR_MANAGED_SAB_QBIT_DOWNLOAD_CLIENT.md), [Download client category gate](../../design/DOWNLOAD_CLIENT_CATEGORY_GATE.md), [ROADMAP Phase 4](../../strategy/ROADMAP.md)

---

## Context

[ADR-0012](0012-qc-import-gate-scope.md) scoped the QC gate to *Mintarr-routed sources only* and deliberately deferred the universal gate. In doing so it rejected, **for now**, "Alternative 2: Mintarr as a universal download-client proxy Lidarr talks to" — on two grounds: it looked like the download-client-management role [ADR-0008](0008-strategic-positioning.md) keeps in Lidarr's territory, and Mintarr is SAB-compatible, not a torrent client, so it could not route Lidarr's torrent grabs anyway. ADR-0012 left an explicit door open: re-evaluation trigger #3 — *"a clean lifecycle handshake is demonstrated for intercepting Lidarr-grabbed content (queue reconciliation + seeding + torrent/usenet handling) that does not require Mintarr to manage download clients in violation of ADR-0008."*

The [Mintarr-managed SAB/qBit lane design](../../design/MINTARR_MANAGED_SAB_QBIT_DOWNLOAD_CLIENT.md) (#169) proposes a **narrow** realization of that handshake: Lidarr talks to Mintarr as the download client for one dedicated music category; Mintarr drives SABnzbd/qBittorrent as **backend transfer engines** for that category, owns the queue identity Lidarr sees, and imports only after QC. It dissolves ADR-0012's torrent objection — Mintarr is not asked to *be* a torrent client to Lidarr; it orchestrates qBit on the backend while staying SAB-compatible to Lidarr.

That design therefore lands directly on ADR-0008's boundary line and reopens what ADR-0012 explicitly parked. This ADR decides whether the scoped backend lane is in bounds, and locks the boundary before any backend-client code is written.

## Decision

**Accept the Mintarr-managed backend lane as an in-scope, category-bounded mechanism — and record it as a deliberate, narrow move of the ADR-0008 line, not a slide into download-client management.**

1. **Mintarr may submit, monitor, and cancel backend SAB/qBit jobs it created**, for exactly one dedicated music category per backend (e.g. `mintarr-music`). This is a *control relationship with jobs Mintarr originated*, which is distinct from — and must never expand into — configuring the operator's download-client ecosystem.

2. **The ADR-0008 boundary is restated, not erased.** Still out of bounds: editing global SAB/qBit configuration, owning or routing Sonarr/Radarr categories, touching any category Mintarr did not create jobs in, and watching shared completed roots. Mintarr's reach is the jobs it submitted into its own category and nothing else.

3. **The QC guarantee for this lane is conditional and must be enforced at onboarding, not merely detected.** "All automatic music imports in this lane pass through Mintarr QC" holds only while Lidarr's *music* download client is Mintarr-only. If Lidarr also has the same SAB/qBit client configured for music, it can import behind Mintarr's back. Onboarding must drive the operator to disable the direct music client and must **fail closed** when it cannot establish the lane is exclusive — the design's "shared paths fail closed" invariant is binding, and the undetectable-misconfiguration case must be surfaced as a precondition, not buried.

   The implementation must distinguish two grades of exclusivity, and must not conflate them:
   - **Machine-verifiable exclusivity** — Mintarr proves it programmatically (e.g. reading Lidarr's download-client config to confirm no direct SAB/qBit client owns the music category, plus category/path containment). Prefer this wherever the APIs allow.
   - **Operator-attested exclusivity** — where Mintarr cannot machine-prove it (ambiguous or insufficiently-exposed config), the operator must *explicitly attest* that the direct music client is disabled, recorded with audit/provenance. The lane proceeds on that attestation, not on a silent assumption.

   "Fail closed" means: absent **either** machine-proof **or** explicit operator attestation, the lane stays disabled. The UI must show which grade is in effect so an attested-only lane is never presented as if it were machine-verified.

4. **The universal gate remains deferred.** This ADR authorizes one scoped lane; it does not gate content Lidarr grabs and imports on its own. ADR-0012's scoping otherwise stands.

5. **Seeding and read-only-source semantics are preserved.** Mintarr copies completed backend files into its managed work/output before QC (never QC-in-place), and never breaks torrent seeding by default.

This decision authorizes the design (#169) and its slices 1–7 to proceed. It does **not** pre-approve any specific env-var names, UI, or backend-client implementation — those remain design/implementation concerns reviewed per slice.

## Rationale

### It is the handshake ADR-0012 asked for, not a boundary collapse

ADR-0012 trigger #3 demanded a lifecycle handshake that intercepts the grab→import cycle *without* managing download clients. The backend-engine framing delivers exactly that: Lidarr↔Mintarr is the only client relationship Lidarr sees, Mintarr owns `jid ↔ backend_job_id`, and the operator's clients keep running for Sonarr/Radarr untouched. Mintarr controls *its own jobs in its own category*, which is a strictly smaller claim than "manage the user's clients."

### It resolves the torrent objection that blocked Alternative 2

ADR-0012 rejected the proxy partly because Mintarr is SAB-compatible to Lidarr and cannot route Lidarr's torrent grabs. Here Mintarr never pretends to be a torrent client to Lidarr; it stays SAB-compatible upstream and drives qBit downstream as an engine. The asymmetry that killed Alternative 2 does not apply.

### The boundary risk is real and is why this is an ADR, not just a design

Submitting and cancelling jobs on a user's qBittorrent is the first time Mintarr *writes/controls* an external transfer engine rather than only reading from it or exposing itself as a client. That is a genuine step past today's read-only-companion posture. Naming it explicitly — scoped to created-jobs-in-one-category, fail-closed, no config edits — keeps it from drifting into the download-client-manager role ADR-0008 forbids. Leaving it implicit in a design doc would let the boundary erode connector-by-connector, which is precisely the drift ADR-0012 was written to prevent.

### The conditional guarantee must be load-bearing, not a footnote

The entire value of the lane is "music imports are QC'd." That guarantee evaporates silently if Lidarr also imports the same backend directly. Treating exclusivity as a fail-closed precondition (not a best-effort detection) is what makes the guarantee honest.

## Consequences

### Positive

- ADR-0012's deferred handshake gets a concrete, bounded path forward; Phase 4 backend work proceeds without re-litigating scope.
- The torrent/usenet asymmetry is handled by orchestration, so qBit music can finally be QC-gated.
- The ADR-0008 boundary is reaffirmed in writing at the exact point of maximum pressure.
- Import-status ownership stays unambiguous (Mintarr owns the lane's `jid`).

### Negative / accepted trade-offs

- **Mintarr now holds a control relationship with a backend engine.** Accepted, but only for jobs it created in one category; this surface must be guarded in every slice (secret redaction, category containment, no global writes).
- **The QC guarantee is conditional on operator config Mintarr cannot fully enforce.** Mitigated by fail-closed onboarding and explicit coverage wording ("Lidarr music routed through Mintarr is gated", never "qBittorrent is protected"), but the undetectable case remains a residual risk that must be documented prominently.
- **Copy-not-move means transient extra disk** (the completed file exists in the backend seed and in Mintarr's work path until cleanup). Accepted as the seeding-safe choice.

## Alternatives considered

### Alternative 1: Keep ADR-0012 as-is; never build the backend lane

Rejected. ADR-0012 itself anticipated this successor; refusing it would leave qBit/SAB music permanently un-gateable except via operator-routed completed folders, under-serving the core positioning for the most common Arr topology.

### Alternative 2: Implement the lane under ADR-0012 without a new ADR

Rejected. The lane crosses from "expose Mintarr as a client / read completed folders" to "control a backend engine's jobs", which is the ADR-0008 pressure point. That boundary decision must be explicit and locked before code, not inferred from a design doc.

### Alternative 3: Go further — universal gate / general download-client proxy now

Rejected, consistent with ADR-0012. This ADR authorizes one category-scoped lane, not interception of Lidarr's arbitrary grabs or management of the client ecosystem.

## Re-evaluation triggers

Re-open or extend when:

1. **The scoped lane proves stable in real operation** (dogfooded end-to-end per design §9 slice 7, import-status ownership holds, no queue-reconciliation or seeding regressions) — at which point widening beyond one category, or toward the universal gate, can be reconsidered against ADR-0012's own triggers.
2. **Onboarding cannot in practice keep the lane exclusive** — if operators routinely end up with Lidarr importing the backend directly despite fail-closed onboarding, the conditional guarantee is too weak and the lane's framing needs revision.
3. **Backend control proves to require global/config writes** to work at all — which would mean the lane cannot stay within ADR-0008 and must be re-decided.

## Out of scope for this ADR

- **Implementation** of the SAB/qBit backend clients, queue state, addurl integration, and ingest (design #169 slices 2–5, reviewed per slice).
- **Exact configuration names and UI** — design/implementation concerns.
- **The universal gate** — remains deferred under ADR-0012.
- **Sonarr/Radarr, and any category Mintarr did not create jobs in** — permanently out.

---

> Locked: 2026-06-13 — accepted by the deciders. Backend-client implementation (design #169 slice 2) may begin within this boundary.
