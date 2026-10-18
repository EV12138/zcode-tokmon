# Changelog

All notable changes to Tokmon are documented here.

## 1.1.1 — 2026-10-18

- Cache hit rate shown after session total, source-specific today, and combined today.
- Cache hit rate breakdown (local / remote) in the details panel.

## 1.1.0 — 2026-10-18

- Model breakdown (`/usage models[:days]`) with cache hit rate and average TTFT.
- Recent request details (`/usage recent[:N]`) with rate, TTFT, retries and tool calls.
- Context window percentage from the ZCode model catalog, with 80% / 95% warnings.
- Dynamic remote target reload when `config.json` or ZCode `setting.json` changes.
- Fixed remote agent deploy when passing stdin (input/stdin conflict).

## 1.0.0 — 2026-10-18

First open-source release.

- Local + SSH remote token usage and generation-rate monitoring.
- MCP tool `token_monitor(scope)`, slash commands `/usage`, `/usage-watch`, `/usage-bar`.
- Optional floating status bar under the composer: rate, session total, context length,
  source-specific today, combined local + SSH today, remote health, details panel.
- Auto-deploy and SHA256-verify the remote agent over SSH; ControlMaster connection reuse.
- Read-only SQLite access; zero client-file modification for core features.
- Rate semantics: output tokens ÷ generation window, reasoning/Claude from `started_at`,
  200 ms–1 h guard, weighted aggregation, 20-row scan-back for last valid rate.
- Data verification: unit tests plus independent SQL cross-checks.
- Status bar injection is opt-in; removal restores `app.asar` byte-for-byte.

## 0.3.x

- Data-layer audit: today/session/context/rate/delta semantics pinned by tests.
- Background remote refresh (SSH latency never blocks the UI).
- SQL aggregation for window totals (no sample truncation).
- SSH subprocess stdin isolation (fixes data-pump EOF restarts).
- Serve restart race fixed (stale exit callbacks no longer clear the live child).
- Portable remote SHA check (`sha256sum` or `shasum`), cached per target.

## 0.2.x

- Per-session routing by exact session id (local and remote databases are separate).
- Context length display.
- Status bar UI rewrite: line icons, hover explanation cards, details panel.
- In-place DOM updates (no layout jitter).

## 0.1.x

- Initial query core, MCP server, terminal dashboard, remote auto-deploy.
