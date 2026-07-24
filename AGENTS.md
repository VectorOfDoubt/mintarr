# Agent instructions

<!-- BEGIN MANAGED ARCHITECTURE BASELINE -->
GENERATED CONTENT — DO NOT EDIT DIRECTLY
Source: policy/agent-architecture-baseline.md
Baseline: ADR-001 v1.2
Release: v1.2.2
Source revision: 90b42b4

## Architecture baseline — rules for AI agents

This project follows **ADR-001 (robust, vendor-independent Git architecture)** and
**ADR-002 (agent instruction distribution)**. Agents MUST respect the rules below. Each
rule is tagged with its criticality. These are guidance; the security/data/legal-critical
rules are ALSO enforced technically — never assume a text rule is the enforcement.

1. **[SECURITY] Forgejo is the canonical target.** The authoritative remote for code AND
   metadata (issues, PRs, releases) is the self-hosted Forgejo instance; `origin` is
   Forgejo. **Transition state:** until Forgejo is stood up (check the repo status), this
   is the *target* state — do not treat any cloud platform as the permanent source of
   truth and do not reintroduce one as a hard dependency. GitHub/GitLab/Codeberg are
   replaceable, mostly read-only mirrors.

2. **[DATA] Mirrors are not backup.** Git mirrors carry commits/branches/tags only — not
   issues, PRs, attachments, registry, config, secrets or full release/LFS data. Never
   propose a mirror as a substitute for an instance backup.

3. **[DATA] Server-side push mirrors, not local multi-push.** Redundancy is fanned out by
   Forgejo, not by adding multiple push URLs on a client. This is a normative redundancy
   and credential-boundary rule, not a project-local workflow preference.

4. **[SECURITY] Least privilege on credentials.** Use project-/repo-scoped, short-lived
   tokens (e.g. GitLab `write_repository`, fine-grained/repo-scoped tokens). Never
   introduce a broad classic PAT. Never hardcode a token or secret.

5. **[SECURITY] Secrets never in Git, never in logs.** Use a secret store / Forgejo
   secrets. A secret MAY be *set* via an authenticated UI, but must never be exposed in
   rendered UI, client bundles, logs, or build artifacts. Redact secrets in any output.

6. **[SECURITY] CI runner is untrusted and isolated.** Workflow code runs arbitrary code.
   It must never have access to Forgejo data volumes, backup storage, or a shared Docker
   socket, and must be network-segmented with least egress. Untrusted PR workflows require
   an explicit trust gate before they run. Pin third-party Actions to reviewed immutable
   commit SHAs and container images to immutable digests where supported.

7. **[DATA] Backups must be consistent, versioned, and verified.** Take a synchronized
   point-in-time snapshot; default stop→snapshot→start (an atomic snapshot while running
   is acceptable only if all Forgejo data is on one snapshottable store). Keep historical,
   time-windowed restore points. A backup is not trusted until a restore has been tested.

8. **[DATA] Offsite means physically elsewhere.** A copy in the same house/building/risk
   zone is a *local* copy, not offsite — even on a separate disk or NAS. Offsite requires a
   different location, separate credentials, and append-only/immutable protection with a
   time-bound retention that a compromised production host cannot subvert.

9. **[LEGAL] Content hygiene.** Classify every repo before mirroring (PUBLIC-SAFE /
   PRIVATE-LEGITIMATE / REVIEW-REQUIRED). Never publicly mirror pirated media, decryption
   keys, tokens, or unclear-rights material. Personal data / unclear-rights content is
   REVIEW-REQUIRED and must not be mirrored before approval — lawful private content may
   still live in Forgejo + encrypted backup. Self-hosting does not make unlawful content
   lawful.

10. **[SECURITY] GitHub is optional and gated.** The architecture works fully without
    GitHub. Do not reintroduce GitHub as a required dependency. A new/replacement GitHub
    account is used only after the account situation is resolved in writing.

11. **[DATA-CRITICAL] Fase 0 before further architecture work.** Uncommitted/untracked
    local work is not protected by mirrors, bundles, or a later Forgejo. A full
    working-tree backup comes first.

12. **[PRINCIPLE] Complexity must be justified.** Prefer few components that are monitored
    and restorable over many that no one maintains.

### Precedence
Baseline is normative. Rules tagged **[SECURITY]**, **[DATA]**, **[DATA-CRITICAL]** or
**[LEGAL]** must **not be weakened** by any project — they may only be *tightened*. A
project may override only **[OPERATIONAL]**/**[PRINCIPLE]** preferences (style, test
commands, layout). Weakening a normative rule requires an **approved ADR waiver**, never a
project-local edit. When unsure whether a rule is critical, treat it as critical.
<!-- END MANAGED ARCHITECTURE BASELINE -->

## Project-specific instructions

<!-- Add rules that apply only to THIS repository below. The sync tool never
     touches anything outside the managed block above. Project rules may add or
     tighten, but must not weaken a normative security/data/legal rule. -->
