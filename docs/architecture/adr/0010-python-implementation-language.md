# ADR-0010: Python as implementation language

**Status:** Accepted — locked 2026-05-31
**Deciders:** Eivind Sjursen, Claude
**Related:** [ADR-0007 No Lidarr fork](0007-no-lidarr-fork.md), [ADR-0008 Strategic positioning](0008-strategic-positioning.md)

---

## Context

The arr-stack (Sonarr, Lidarr, Radarr, Prowlarr, Readarr, Bazarr) is implemented in C# / .NET. Mintarr positions itself as an arr-stack companion ([ADR-0007](0007-no-lidarr-fork.md), [ADR-0008](0008-strategic-positioning.md)) and operators reasonably ask "why isn't it C# too?"

Mintarr was written in Python from the start (`tidalhires` predecessor, ~5,000 LOC Python 3.12 Flask application). The language choice was never formally locked. With Phase 0 cutover approaching, leaving this implicit invites "let's rewrite in C# for arr-stack consistency" or "let's translate to Rust for performance" proposals years from now — both expensive conversations to re-have.

This ADR locks Python as the implementation language for Mintarr's core runtime and tests, and records why.

## Decision

**Mintarr is implemented in Python 3.12+.** Core runtime, tests, and public contract/cutover scripts are Python unless an explicit ADR overrides for a specific subsystem.

The dashboard frontend is currently server-rendered HTML/CSS/vanilla JS (no JavaScript framework). Phase 2 redesign may introduce a frontend framework, which would be a separate ADR. The Python runtime is not affected by that choice.

## Rationale

### Maintainer fit

- Eivind's primary language is Python. C# would be a multi-year ramp.
- Claude and Codex generate Python more reliably than C#. AI-assisted contribution remains a load-bearing maintenance strategy for a small-team open-source project.
- Community-PR audience for self-hosted music and media tooling is Python-comfortable (Beets, Picard plugin tooling, AcoustID wrappers, yt-dlp-style utilities, and many small Docker-first operator tools).

### Domain library access

- Audio processing libraries in Python are mature and widely used: `librosa`, `scipy.signal`, `numpy`, `mutagen`, and `essentia`. `pydub` remains useful as a legacy audio-wrapper utility but is not a strategic dependency. Future in-house audio QC ([Phase 8 direction noted in ROADMAP](../../strategy/ROADMAP.md)) builds on this stack.
- HTTP client / server, SQLite, subprocess management, threading — all first-class in standard library or well-maintained third-party packages.
- ML/DSP advances in 2025-2026 land in Python first; if Mintarr ever wants AcoustID-style audio fingerprinting or perceptual entropy heuristics, Python keeps that path open.

### Codebase scale

- Mintarr's application runtime is about 7,400 lines of Python today, with about 12,300 lines including tests and public scripts. Flask + SQLite hits the sweet spot for this scale.
- ASP.NET would be appropriate if Mintarr were a 50,000+ LOC application with multiple deployment targets and enterprise auth requirements. It is not.
- Smaller language stack = lower friction for contributors and easier debugging.

### Protocol-level interoperability

Mintarr does not need to call into arr-stack code. It exposes itself to Lidarr through external protocols:

- Newznab indexer (XML over HTTP)
- SAB-compatible download client (JSON over HTTP)

These are language-agnostic. Lidarr does not care that Mintarr is Python any more than it cares what language an indexer service runs.

### Discovery and community signal

- Arr-stack discovery happens through function ("Lidarr companion / QC layer"), not through implementation language. Operators install whatever runs in their Docker stack.
- Self-hosted-tool community treats language as orthogonal to credibility. Python-backed tools such as Mealie, Paperless-ngx, Beets and yt-dlp-style utilities achieve high adoption alongside Go/C#/Rust alternatives.

### Performance is not a blocker

- The pipeline is I/O-bound: subprocess to `tidal-dl-ng`, HTTP to FLAC Detective, file copy, HTTP to Lidarr. The Python overhead between these calls is negligible.
- The worker queue is N=1 by design (see worker-queue design notes; full F2 design doc to be migrated in v0.2.0). Single-thread concurrency is not a Python-specific limitation.
- If a specific hot path ever needs native performance, Python's C extension or subprocess-to-native-binary pattern handles it without rewriting the host application.

## Consequences

### Positive

- Codebase remains accessible to existing maintainers and AI assistance
- Python library ecosystem available for future audio-DSP work, ML-based heuristics, advanced metadata identity
- Container images stay small (Python 3.12-slim base, no .NET runtime)
- New contributors with Python background can ramp in hours, not weeks
- Existing test suite, tooling (ruff, mypy, pytest), and CI conventions remain unchanged

### Negative

- Operators expecting "C# for arr-stack consistency" need an explanation. This ADR is that explanation.
- We do not benefit from Lidarr's existing helper libraries (their MusicBrainz client, their indexer abstractions). Mintarr re-implements what it needs from scratch. Mostly Mintarr does not need them ([ADR-0008 boundary test](0008-strategic-positioning.md) keeps Mintarr out of Lidarr's territory).
- AOT compilation, single-binary deployment, native-binary distribution — all not available. Mintarr is container-first. Operators wanting bare-metal install run Python directly; not a v1 supported path.

### Accepted trade-offs

- Compiled-language enthusiasts may consider Mintarr's choice a missed opportunity. The trade-off favours maintainer velocity and community-PR friction over theoretical performance.
- The runtime cost of Python startup (~200ms gunicorn boot) is acceptable because Mintarr is a long-running service, not a CLI tool.

## Alternatives considered

### Alternative 1: C# / .NET (match arr-stack)

Rejected. Multi-year ramp for the maintainer team. No protocol-level benefit because Mintarr does not call into Lidarr's process. The "arr-stack consistency" framing is aesthetic, not functional.

### Alternative 2: Go

Considered. Strong concurrency story, single-binary distribution, good HTTP performance. Rejected because:

- Maintainer team less fluent than in Python
- Audio-DSP library ecosystem weaker
- Adapter contract design assumes Python idioms (Protocols, dataclasses) that have less natural Go equivalents

### Alternative 3: Rust

Rejected. Excellent for performance-critical systems but Mintarr is I/O-bound. The borrow-checker friction for what is essentially a Flask-and-SQLite application would slow the project significantly. The performance ceiling Mintarr could reach with Rust is one we do not need to reach.

### Alternative 4: TypeScript / Node.js

Rejected. Strong frontend story but the backend ecosystem for audio processing, subprocess control, and SQLite is weaker than Python's. Mintarr's frontend choice is independent (Phase 2 separate ADR if needed).

## Re-evaluation triggers

This ADR is re-opened only if:

1. **Python 3.12+ enters end-of-life with no migration target** that Mintarr's dependencies can follow. Extremely unlikely within ten years.
2. **The maintainer team shifts to non-Python primary expertise.** Hypothetical; not a current condition.
3. **A specific subsystem demonstrates a performance ceiling Python cannot reach** that operators report as a problem in practice. The subsystem-specific solution (C extension, subprocess to native binary) is the first response; whole-application rewrite is the last response.
4. **The audio-DSP ecosystem shifts to a different primary language.** No signal of this happening.

Until then, ADR-0010 stands. Proposals to rewrite Mintarr in another language are closed with a reference to this ADR and an invitation to propose a successor ADR addressing the triggers above.

## Out of scope for this ADR

- **Frontend framework choice.** Currently vanilla HTML/CSS/JS. Phase 2 redesign may introduce a framework. Separate ADR if/when needed.
- **Per-subsystem language exceptions.** A future C extension, Cython optimisation, or subprocess-to-Rust binary for a specific hot path does not contradict this ADR — it adds a Python-orchestrated component.
- **Tooling language.** Public Mintarr contract/cutover scripts under `scripts/` are Python. CI shell scripts are bash. Build configuration is YAML. Private Windows incident/debug helpers may use PowerShell because they target Windows diagnostics, not Mintarr runtime. These are not "implementation language" choices.

---

> Locked: 2026-05-31
