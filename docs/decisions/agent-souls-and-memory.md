# Agent souls and memory

**Status:** Parked — designed, Phase 1 ready to start.

## Goal

Give Sonic (WhatsApp) and SuperSonic (Discord) **consistent personalities** ("soul") and **persistent per-sender memory** that survives beyond the SDK's 12-hour live-session TTL. Model the design on OpenClaw's memory pattern (short-term → promote → long-term, RAG chunks, REM/dreaming) but implement it entirely in our own codebase. OpenClaw's native memory system is not wired in — it was explicitly out of scope.

## Sender identity (resolved 2026-04-22)

Earlier versions of this doc flagged per-sender memory in groups as blocked by OpenClaw not passing sender identity. That was wrong. OpenClaw embeds a JSON metadata block at the start of each user message containing `sender_id` (E.164 phone), `sender` (display name), `conversation_label`, `is_group_chat`, and `was_mentioned`. See `openclaw-migration.md` for the full shape. The bridge will parse that block and use `sender_id` as the memory key. No WAHA migration required.

## Three clocks worth distinguishing

| Clock | What it is | Retention |
|---|---|---|
| Session TTL (`SESSION_MAX_AGE = 12h`) | Live Claude SDK subprocess holding raw conversation | 12h idle → reaped. Kept as-is. |
| Memory file (`MEMORY.md` per peer, added Phase 1) | Distilled facts on disk | Permanent |
| `short_term.jsonl` (added Phase 2) | Pending facts awaiting promotion | ~30d; promoted or expired |

Extending the session TTL to a week was considered and rejected — each live session holds ~150 MB RAM per peer and inflates cache-read tokens every turn, and memory belongs in a file anyway.

## Decisions locked in

1. **Per-peer scope.** Memory is keyed by peer hash. One user's memory never visible to another.
2. **Agent-wide "house views"** (e.g., "Sonic avoids penny-stock pumping") live in the soul file, not per-peer memory.
3. **Siloed across agents.** Sonic's memory of a user does not flow into SuperSonic's and vice versa — social/context boundary.
4. **Self-contained storage.** No dependency on OpenClaw's memory SQLite or files. Pure `logs/memory/` + `prompts/` inside this repo.
5. **Session TTL stays at 12h.** Memory is the cross-session mechanism; stretching the session isn't.

## Phased plan

### Phase 1 — Soul + long-term memory only (~4h)

- `prompts/sonic_soul.md`, `prompts/supersonic_soul.md` — agent personality, quirks, humor, vocab boundaries. Git-tracked. Loaded into the system prompt every turn.
- `logs/memory/<agent>/<sender_id>/MEMORY.md` — per-sender facts, markdown, injected into the system prompt after the soul block. `sender_id` is the E.164 phone parsed from OpenClaw's metadata block (WhatsApp) or the Discord user id (Discord).
- New `remember(fact)` tool — appends to the sender's `MEMORY.md` with timestamp + light dedup.
- Bridge and Discord bot both read/write per-sender files but in **separate per-agent trees** (siloing — `logs/memory/sonic/...` vs `logs/memory/supersonic/...`).
- Fallback: if metadata parsing fails (malformed JSON, absent block), fall back to the existing hash-of-first-message peer key and continue without memory. Never crash a turn on memory errors.

After Phase 1, Sonic has a voice, and remembers things like "long-term investor, owns SCHD/VOO, dislikes options" per actual person — Bala, KP_NeverQuits, Boss, Ayaps each have their own memory regardless of whether they DM or @-mention in groups.

### Phase 2 — Short-term + promotion (~4h)

- Extend `remember(fact, kind="short"|"long")`. Short entries go to `short_term.jsonl`.
- `tools/memory_promote.py` — scores short-term entries by (recency, repetition, specificity); top N get appended to `MEMORY.md`. Mirrors OpenClaw's `memory promote`.
- Nightly systemd timer.

### Phase 3 — Embeddings + semantic recall (~1 day)

- Per-peer `chunks.sqlite` with a schema matching OpenClaw's (`files` / `chunks` / `embedding_cache` / `chunks_fts`).
- Local embedding model (to avoid new API cost).
- New `recall(query)` tool — semantic search over past excerpts, invoked when a message hints at earlier context.

### Phase 4 — REM / dreaming (~0.5 day, optional)

- Nightly timer during low-traffic hours summarizes each active peer's last 24h, extracts candidate facts, enqueues to `short_term.jsonl`. Promotion then decides what goes long-term.
- Mirrors OpenClaw's `rem-backfill`.

## Open questions at kickoff

When this project resumes:

1. **Privacy/review command**: add a user-facing "what do you remember about me?" so peers can inspect their own memory? (Recommended: yes — builds trust, ~30 min.)
2. **Bootstrapping**: seed `MEMORY.md` for known peers with a couple of facts up front, or start empty and build organically?
3. **Per-group memory**: OpenClaw routes group messages. Should group-chat behavior build its own memory keyed by group JID instead of sender? (Default: no — DMs only. Groups have too many voices.)

## Decision

**Parked** — Boss wants to avoid disrupting live agents mid-day. Phase 1 first, measure UX lift, decide whether to build Phases 2–4.

Estimated total if all phases ship: ~2.5 engineering days. Phase 1 alone is expected to be the 80% win.
