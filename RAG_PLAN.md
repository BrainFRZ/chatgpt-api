# RAG System Plan

## Status

**NOT YET IMPLEMENTED.** This is a plan held against future need.

Do not build this until context bloat from injected reference material becomes a felt pain in real sessions. The design is ready when the problem is real. Pre-optimization is the root of all evil.

---

## Context

Long narrative chats (TTRPG sessions, Novels) accumulate context bloat from injected reference material — published modules, lore documents, NPC bios, stat blocks. Naive solutions all have failure modes:

- **Load everything upfront**: blows context budget, costs full input price every turn.
- **Aggressive summarization**: loses precision, irreversible.
- **Sliding window only**: cuts at arbitrary message boundaries, can't handle reference material at all.

This system brings retrieved content into Opus's context when it's relevant and removes it cleanly when it's not, without losing continuity, without spending tokens re-injecting every turn, and without adding LLM calls to the retrieval loop.

The conversation itself is **not touched** by this system — Chorus's existing rolling sawtooth window manages conversation history. RAG chunks have an independent lifecycle that runs alongside it.

---

## Architecture Overview

```
                    ┌──────────────────────────────────┐
                    │  Source Material (PDFs, .md)     │
                    └────────────┬─────────────────────┘
                                 │ (one-time, indexing)
                                 ▼
                    ┌──────────────────────────────────┐
                    │  Indexing Pipeline               │
                    │  Opus 4.6 split → chunk → embed  │
                    └────────────┬─────────────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────────────┐
                    │  LanceDB Corpus Table            │
                    │  per-project, vectors + BM25     │
                    └────────────┬─────────────────────┘
                                 │
   ┌─────────────────────────────┼─────────────────────────────┐
   │ Per turn (runtime):                                       │
   │                                                           │
   │  User msg ──▶ Backend retrieves (vector + BM25)           │
   │              against rolling window query                  │
   │              ─▶ Diff against live_chunks                  │
   │              ─▶ Apply hysteresis                          │
   │              ─▶ Inject newcomers, evict departures         │
   │              ─▶ Render forced + live chunks                │
   │              ─▶ Send to Opus (or pipeline agents)          │
   │                                                           │
   │  ZERO LLM calls in this loop. Pure backend logic.         │
   └───────────────────────────────────────────────────────────┘
```

**Per turn at runtime:** zero LLM calls for retrieval. One Opus call (single-agent) or 1–3 pipeline calls (multi-agent) for generation. Retrieval is plumbing.

---

## Components

### 1. Vector Store: LanceDB

Embedded vector database, file-based, no separate service. `pip install lancedb`, point at a directory.

**Why LanceDB over alternatives:**

- Native hybrid search (vector + BM25) in one query — better retrieval than vector-only, especially for proper nouns and exact matches.
- Native reranking pipeline support, available when needed.
- Purpose-built for this exact use case; clean Python API shaped around vector + filter + retrieve workflows.
- Embedded, file-based, fits Chorus's existing "no extra services" character.
- Designed to scale far beyond what we'll ever need; no growth ceiling.

**Why not sqlite-vec:** Hybrid search is more work to assemble (FTS5 + vec extension stitched via SQL). Acceptable but more friction. The integration cost is identical (`pip install`), so take the better tool.

**Why not Chroma, pgvector, Qdrant, etc.:** Chroma has stability history; pgvector requires Postgres infrastructure; Qdrant server mode adds a process to manage. None justify the added cost over LanceDB for solo use.

### 2. Embedding Model: voyage-3-large

- 1024 dimensions.
- ~$0.18/M tokens.
- Best-in-class retrieval quality on benchmarks.
- Voyage is Anthropic's recommended embedding partner for use with Claude.
- New SDK to integrate (`voyageai` package), one-time cost.

**Why not OpenAI text-embedding-3-small:** "Plenty good" is not a reason to pick a worse tool when cost is rounding error and quality regressions are silently undetectable. Don't start with the cheap option only to discover three months in that something was being missed and not know it.

**Why not text-embedding-3-large:** If we're going to spend integration effort on a quality upgrade, spend it on the actual best, not the second-best that happens to share an API key.

**Embedding cost reality check:** Indexing a full module like LMoP costs ~$0.02 one-time. Per-session query cost is fractions of a cent. Embedding cost is invisible at this scale; pick on quality, not price.

### 3. Chunking Pipeline

Two-stage approach that respects the dual nature of TTRPG source material.

**Stage 1: Split source into mechanical and narrative streams (Opus 4.6 on claude.ai).**

PDFs and module markdown contain two intermixed kinds of content with different ideal chunking strategies:

- **Mechanical**: stat blocks, random tables, treasure listings, room mechanics, traps, hard rules. Has hard atomic boundaries.
- **Narrative**: read-aloud text, lore, NPC personalities, history, scene descriptions. Has fuzzier topical boundaries.

Run the source PDF through Opus 4.6 with a system prompt that produces two parallel `.md` files: `<source>_mechanical.md` and `<source>_narrative.md`. Cross-reference points where a narrative section corresponds to a mechanical one.

**Stage 2: Chunk each stream with the right strategy.**

- `mechanical.md` → **structural chunker** (deterministic, headings + size guardrails). Each stat block, table, room key = one chunk.
- `narrative.md` → **semantic chunker via Opus 4.6**. Pass it the file, ask for topical chunks of ~500–1500 tokens each, output as JSON list. One Opus call per source, costs a few dollars one-time.

**Chunking rules:**

- **Maximum chunk size:** ~1500 tokens. If a section is bigger, split on next-level subheadings.
- **No minimum chunk size.** Small chunks are fine if the content is small. Forcibly merging tiny sections with siblings produces muddier vectors and dumps unrelated content into context on retrieval.
- **Drop literal-noise chunks** under ~20 tokens (heading-only artifacts, list separators).
- **Never split mid-stat-block.** Stat blocks are atomic.
- **Stable chunk IDs**: hash of `(source, heading, position)` so re-indexing the same source produces the same IDs and existing chat references don't break.

**Stage 3: Contextual retrieval preamble (per Anthropic's 2024 paper).**

For each chunk, generate a 1–2 sentence contextual preamble describing how the chunk fits in the larger document, via a cheap LLM call. Concatenate `preamble + content` and embed *that* combined text. The chunk's stored `content` is unchanged; only its `embedding_text` differs.

Anthropic's paper claims ~35% reduction in retrieval failures. Cost: a few dollars per source at indexing time, one-time. Bake it in from v1 because it's not reversible without re-embedding the corpus, and quality regressions from skipping it would be silently undetectable.

**Stage 4: Embed and store.**

Each chunk → voyage-3-large → 1024-dim vector → write to LanceDB along with metadata.

**Chunk default routing (assigned by chunker):**

- Stat block, random table, treasure, room mechanics → `agents: ["mechanics"]`
- Read-aloud, scene description, lore, history → `agents: ["events", "narration"]`
- **NPC bio** → `agents: ["events", "mechanics", "narration"]` (Mechanics needs personality for skill DCs)
- **Magic item** → `agents: ["events", "mechanics", "narration"]` (lore + effects)
- **Faction description** → `agents: ["events", "mechanics", "narration"]`

The chunking LLM has discretion to elevate borderline chunks to multi-agent routing.

### 4. Retrieval (deterministic, no LLM in the loop)

**Per turn, the backend:**

1. Builds a query from the **rolling sawtooth window**, capped at ~16k tokens. Take the most recent N turns that fit. (Sawtooth window is typically 20–40 turns; capping at 16k respects voyage-3-large's effective embedding context.)
2. Embeds the query with voyage-3-large.
3. Runs **hybrid search** against the project's LanceDB corpus table: vector similarity + BM25, weighted combination, top **K=8** candidates.
4. Computes the new `live_chunks` set by applying hysteresis (see §5) against the previous turn's `live_chunks`.
5. Forces in any `forced_chunks` (see §6).
6. Renders the resulting set into Opus's context (see §Pipeline Interaction).

**Cost per turn:** an embedding call (~1500 tokens query × $0.18/M ≈ $0.0003) plus local LanceDB operations (free). Negligible.

**Query construction:**

The query is the user's new prompt + the recent conversation window. **Not** the chunks already in context (would just keep retrieving what's already there).

**OOC handling:** implicit. Vector search on OOC text won't match in-game chunks well; threshold filters them out naturally. No special OOC short-circuit needed for v1.

**Empty case:** if nothing crosses the threshold this turn, nothing new gets injected. Currently-live chunks remain via hysteresis. The model gets the conversation as-is. Valid result.

### 5. Hysteresis

To prevent cache thrashing from chunks oscillating around a single threshold:

- **T_in** (enter): higher bar. A candidate must score ≥ T_in to be newly injected.
- **T_out** (stay): lower bar. A live chunk stays live as long as it scores ≥ T_out.
- **N consecutive turns below T_out** before eviction (optional temporal stickiness).

Initial values are guesses; **tune empirically** via the post-session tuning skill (see §Operations). Sweep against hand-labeled ground truth from real sessions.

### 6. Forced Chunks

**Single concept** (formerly two: "sticky" and "pinned"). Each forced chunk has **per-agent expiry counters**, not one shared counter, because chunks need to be pinnable to different agents independently with different lifetimes.

**Per-agent counters:**

- `single` — used in single-agent flow (Anthropic models: Opus, Sonnet, Haiku via `main.py`)
- `events` — Events stage of the standard 3-stage pipeline AND Planning stage of the 2-stage mode pipeline (combat/hack/net_combat) — they share a role, so they share a column
- `narration` — Narration stage in both pipeline types

**Mechanics does not consume RAG chunks at all**, in any form:

- For game systems with `deterministic_mechanics: True` (cpred): Mechanics is pure backend Python (`resolve_pipeline_mechanics` → `cpred_mechanics.resolve_actions`). No LLM call, no docs, no chunks.
- For game systems with a `mechanics_contract` (dnd5e_cyber, until migrated): Mechanics is an LLM agent that receives its existing static doc assignments via `agent_files["mechanics"]`. RAG chunks do not route to it. The migration plan is to convert these to deterministic too, after which Mechanics has no LLM presence anywhere.
- In single-agent flow (`main.py`): Opus invokes mode-specific deterministic tools (`combat_tool`, `hack_tool`, `net_combat_tool`, `ship_combat_tool`, `report_state`) mid-generation. These resolve in backend Python — no chunk consumption.

The popup has no Mechanics column for this reason. Mechanics is mechanical, deterministic, and rule-based — backend code is the right surface, not user-curated chunk routing.

**Counter semantics (each cell):**

- `-1` → permanent. Never decremented. Removed only by explicit GM action.
- `0` → inactive/expired. Not pinned for this agent. Entry is dormant — still in the list but contributing nothing.
- positive `N` → decremented each turn, becomes 0 when expired.

After each turn, the backend decrements all positive counters across all entries. Forced chunks bypass retrieval scoring entirely — they're always in the live set for whichever agents have non-zero counters, regardless of vector/BM25 results.

**No automatic deletion of dormant entries.** When all per-agent counters reach 0, the entry stays in the chat's `forced_chunks` list as a *dormant* entry — the backend ignores it at routing time, but it remains as a record that this chunk was previously pinned in this chat. This serves as a "recently used in this chat" history, letting the GM re-activate it with one click without re-searching the corpus.

The popup hides dormant entries by default with a "show inactive (N)" toggle to reveal them. Click any dormant entry's counter cell to re-activate. **Nothing is ever deleted from LanceDB** — only the chat-level entry's counters change.

**Two management surfaces** (both edit the same `rag_state.forced_chunks` state):

1. **Forced Chunks popup (in-app UI, primary).** Detailed spec in Operations. The normal way to manage pins.
2. **Claude Code workflow (scripting/bulk).** Useful for power-user operations: bulk re-stickying after a corpus re-index, scripted setup from a template, editing project-wide defaults in `rag_config.json`. Coexists with the popup; both edit the same state file.

Neither is more "correct" than the other; they're different ergonomic surfaces over the same underlying data.

**Per-project defaults**: each project has a `rag_config.json` with a starter forced-chunks set that gets copied into new chats on creation. Editable via Claude Code (text file) or via a "Save current forced chunks as project default" button in the popup.

---

## Data Model

Three storage layers, each holding a different kind of thing.

### Layer 1: Corpus (LanceDB, per project)

One table per project, read-mostly. Built at indexing time, queried at retrieval time.

```
Table: project_<project_id>_corpus

chunk_id            string  (primary key, e.g., "lmop_room_07")
content             string  (the chunk text — what gets rendered into Opus's context)
contextual_preamble string  (LLM-generated context prefix)
embedding_text      string  (preamble + content — what was embedded)
embedding           vector(1024)
content_kind        string  ("mechanical" | "narrative" | "mixed")  -- descriptive only
agents              list[string]  -- routing key, e.g., ["events", "mechanics", "narration"]
source              string  ("lmop")
parent              string  ("Cragmaw Hideout")
heading             string  ("Room 7: Goblin Den")
names               list[string]  -- NPCs, locations, items mentioned (for BM25/filters)
tags                list[string]
gm_only             bool
token_count         int
```

`agents` is the routing field. `content_kind` is descriptive metadata for facets/search/the GM's mental model.

### Layer 2: Chat-level RAG state (inside chat JSON)

New top-level field on the existing chat JSON file. Small (<20 entries typical). Loaded with the chat.

```json
{
  "messages": [...],
  "stats": {...},
  "pipeline_state": {...},
  "rag_state": {
    "project_corpus_table": "project_lmop_corpus",
    "live_chunks": [
      {
        "chunk_id": "lmop_room_07",
        "host_message_id": "msg_uuid_42",
        "injected_at_turn": 12,
        "last_combined_score": 0.67,
        "consecutive_below_t_out": 0
      }
    ],
    "forced_chunks": [
      {
        "chunk_id": "lmop_spine",
        "single": -1, "events": -1, "narration": -1,
        "starred": true
      },
      {
        "chunk_id": "lmop_campaign_state",
        "single": -1, "events": -1, "narration": -1,
        "starred": true
      },
      {
        "chunk_id": "lmop_npc_klarg",
        "single": 5, "events": 5, "narration": 0,
        "starred": false
      }
    ]
  }
}
```

**Per-agent counter fields**: `single`, `events`, `narration`. `-1` = permanent, `0` = unset/expired, positive = turns remaining (decremented each turn). Mechanics has no field — it's backend-managed. See §Components / Forced Chunks.

**Starred** field: boolean, surfaces the chunk to the top of the Forced Chunks popup for easy access. Independent of pin state.

### Layer 2b: Per-message chunk attachments

Messages reference chunks **by ID**, not by embedded content:

```json
{
  "id": "msg_uuid_42",
  "role": "user",
  "content": "I draw my sword and step forward.",
  "attached_chunks": ["lmop_room_07", "lmop_npc_klarg"],
  "timestamp": "..."
}
```

**Critical design property**: chunk content is rendered into the API request at send-time by looking up each ID in LanceDB and prepending a `<context>...</context>` block. The message's `content` field always represents what the user actually wrote.

Why this matters:

- Eviction = remove an ID from a list. No text munging.
- Re-homing = move an ID from one message's list to another's.
- No content duplication; chunks stored once in LanceDB, referenced by ID.
- Inspecting a chat file shows the conversation, not the conversation tangled with retrieved context.
- Caching still works: same `attached_chunks` list + same content = same rendered bytes = same cache prefix.

**Live vs attached invariant**: every entry in `rag_state.live_chunks` should also be present in exactly one message's `attached_chunks` (its host) — except for `forced_chunks`, which have no host (they're rendered separately, see §Pipeline Interaction). Worth a backend assertion.

### Layer 3: Retrieval log (sidecar JSONL, separate file)

The retrieval log gets large over a long campaign. Bloating chat JSON would slow chat loading. Goes in a sidecar.

File: `<chat_id>.rag_log.jsonl`, append-only.

```jsonl
{"turn": 12, "timestamp": "...", "query_token_count": 14823, "candidates": [{"chunk_id": "lmop_room_07", "vector_score": 0.71, "bm25_score": 0.45, "combined_score": 0.62, "above_t_in": true, "decision": "inject"}, ...], "live_before": ["lmop_npc_sildar"], "live_after": ["lmop_npc_sildar", "lmop_room_07"], "evicted": [], "rehomed": []}
```

One line per turn. Read by the tuning skill (see §Operations); never loaded during normal chat operation.

### Layer 4: Per-project rag_config

Small file per project, read by backend at chat creation to seed `rag_state.forced_chunks`.

```json
// projects/lmop/rag_config.json
{
  "default_forced_chunks": [
    {
      "chunk_id": "lmop_spine",
      "single": -1, "events": -1, "narration": -1,
      "starred": true
    },
    {
      "chunk_id": "lmop_campaign_state",
      "single": -1, "events": -1, "narration": -1,
      "starred": true
    },
    {
      "chunk_id": "lmop_phandalin_overview",
      "single": -1, "events": -1, "narration": -1,
      "starred": false
    }
  ],
  "global_starred_chunks": [
    "lmop_spine",
    "lmop_npc_sildar",
    "lmop_phandalin_map"
  ]
}
```

**Two related but distinct fields:**

- `default_forced_chunks` — a starting set of *pinned* chunks (with per-stage counters) copied into a new chat's `rag_state.forced_chunks` at chat creation. Affects the new chat only at the moment it's created; subsequent edits diverge per chat.
- `global_starred_chunks` — a list of chunk IDs that are *globally starred* for the project. Surfaces those chunks at the top of the Forced Chunks popup in every chat in the project, regardless of whether they're pinned in that chat. Applies live to all chats; editing this list immediately affects every chat's popup display.

Editable via Claude Code (text editor) or via the popup's "Save current as project default" action and the global-star double-click gesture.

### Schema migration

Existing chats lack `rag_state` and `attached_chunks`. Backend treats absence as "no RAG state, render messages with empty attached_chunks." Old chats work unchanged. New chats opt in by being associated with a project that has a corpus.

---

## Pipeline Interaction

The RAG layer is mostly invariant across all of Chorus's flows. There are **three flows** to consider:

1. **Single-agent flow** (`main.py`) — Anthropic models (Opus, Sonnet, Haiku) with optional mode tools for combat/hack/net_combat/ship_combat
2. **Standard 3-stage pipeline** (`run_pipeline`) — GPT-5.4 general gameplay, Events → Mechanics → Narration
3. **2-stage mode pipeline** (`run_mode_pipeline`) — GPT-5.4 combat/hack/net_combat modes, Planning → Backend Resolution → Narration

The unifying rule across all three: **chunks route to LLM stages that need narrative context. Mechanics never gets chunks regardless of which form it takes.**

### Single-agent flow (Anthropic models)

- **0 LLM calls for retrieval**, 1 Opus call for generation per turn.
- One cache, one consumer.
- All live chunks for the `single` column are rendered into Opus's context.
- `forced_chunks` (with `single != 0`) rendered as a `<context>` block at the very top of the conversation, before the first message — stable across the session, sits in the cache prefix permanently, never invalidates.
- Auto-retrieved chunks attached to host messages via `attached_chunks` ID lists, rendered in-place at send-time.
- Re-homing at sawtooth roll-off boundaries: when a message is about to roll off the window, any of its still-live `attached_chunks` get re-homed to the latest message. Free at that moment because the host is leaving anyway.
- **Mode tools** (`combat_tool`, `hack_tool`, `net_combat_tool`, `ship_combat_tool`, `report_state`) that Opus invokes mid-generation are deterministic backend Python and consume no chunks. RAG is unaffected by them.

### Standard 3-stage pipeline (Events → Mechanics → Narration)

- **0 LLM calls for retrieval**, 1–3 model calls for generation (Events always, Mechanics often if LLM-mode, Narration usually).
- One retrieval pass per turn, results routed to LLM stages that take chunks (Events and Narration).
- Per-agent caches invalidate independently — narrower blast radius than single-agent.

**Routing for Events and Narration (the chunk-consuming stages):**

Forced chunks: routed by their per-agent counters. A chunk with `events: 5` is pinned to Events for 5 turns; a chunk with `narration: -1` is permanently pinned to Narration. Mixed states (Events pinned but Narration not) are valid and supported.

Auto-retrieved chunks: routed based on the chunk's `agents` eligibility list (set at indexing time). A chunk with `agents: ["events", "narration"]` is eligible for both; one with `agents: ["events"]` only goes to Events.

```
chunks_for_stage(stage_name in {"single", "events", "narration"}) = [
    forced_chunk for forced_chunk in forced_chunks
    if forced_chunk[stage_name] != 0
] + [
    auto_chunk for auto_chunk in live_chunks
    if stage_name in auto_chunk.agents
]
```

**Mechanics in the 3-stage pipeline:**

- **Deterministic mode** (`deterministic_mechanics: True`, e.g., cpred): no LLM call, no docs, no chunks. Pure Python via `cpred_mechanics.resolve_actions`. RAG has nothing to do with it.
- **LLM mode** (has `mechanics_contract`, e.g., dnd5e_cyber until migrated): runs as an LLM agent and receives its existing static doc assignments via `agent_files["mechanics"]`. **RAG chunks do not route to it.** The static doc system handles it the same way it handled it before RAG existed. Migration to deterministic is the long-term plan; until then, LLM Mechanics keeps using static docs.

Either way, the popup has no Mechanics column.

### 2-stage mode pipeline (Planning → Backend Resolution → Narration)

Used for combat / hack / net_combat modes specifically. Structure:

| Stage | What it does | Consumes RAG chunks? |
|---|---|---|
| **Planning** (LLM, non-streaming JSON) | Proposes actions and state updates given the current mode context | **Yes — uses the `events` column.** Planning is mechanically the same role as Events: "what's happening, propose actions." Routes the same chunks. |
| **Backend Resolution** (deterministic Python) | `resolve_actions()` on the Planning JSON's actions array | No. Pure Python, no model. |
| **Narration** (LLM, streaming) | Writes prose from resolved actions | **Yes — uses the `narration` column.** Same as the standard pipeline's Narration stage. |

**The key insight:** Planning and Events serve the same role from RAG's perspective. Both are "the LLM stage that does narrative analysis and proposes actions." The popup's `events` column drives both. We don't need a separate `planning` column.

When a 2-stage mode pipeline runs, the backend builds Planning's request with `chunks_for_stage("events")` and Narration's request with `chunks_for_stage("narration")` — exact same logic as the 3-stage pipeline, just with one fewer LLM stage in the middle and a deterministic resolver in its place.

### How chunks attach to each stage's context (varies by stage shape)

| Stage | Context shape | Chunk rendering |
|---|---|---|
| **Single** (main.py) | Full rolling sawtooth window | `attached_chunks` per host message + forced chunks at top |
| **Events / Planning** | Full rolling window (Events) or mode-specific message list (Planning) | `attached_chunks` per host message (Events) or rendered as a context block (Planning) + forced chunks at top |
| **Narration** | Last 20 user-assistant pairs + previous-stage JSON | `attached_chunks` per host message within the 20-pair window + forced chunks at top |
| **Mechanics (LLM mode)** | Stateless per turn (Events JSON + static docs only) | **No RAG chunks at all.** Static `agent_files["mechanics"]` only. |
| **Mechanics (deterministic) / Backend Resolution / mode tools** | Pure Python | Not applicable — no LLM stage |

### Cache implications

- **Always-permanent forced chunks** (counter `-1` for a column) sit in that stage's stable cache prefix (alongside system prompt and project files). Never invalidated. Pure cache wins.
- **Time-limited forced chunks** (positive counter for a column) invalidate the cache of that stage each time the set changes (a counter expires, a new pin is added).
- **Auto-retrieved chunks** invalidate per-stage caches scoped to which stages consume them. A new chunk eligible only for `events` only invalidates Events' cache.
- **In single-agent flow**, all chunk mutations invalidate the one cache.
- **Mechanics caches (LLM mode) are unaffected by RAG** — they only invalidate when the static doc set changes, just as before.

---

## Operations

### Indexing pipeline (one-time per source)

A separate command, not part of chat flow. Roughly:

1. **Source prep**: convert PDF to text/markdown if needed.
2. **Split**: Opus 4.6 on claude.ai → `<source>_mechanical.md` and `<source>_narrative.md`.
3. **Chunk mechanical**: deterministic structural chunker (headings + size guardrails).
4. **Chunk narrative**: Opus 4.6 → JSON list of topical chunks.
5. **Assign agents**: chunker assigns default `agents` list per chunk type, with discretion for cross-cutting types.
6. **Generate contextual preambles**: per-chunk LLM call (cheap model — Haiku 4.5 or similar — fine here since it's one-time).
7. **Embed**: each `preamble + content` → voyage-3-large → 1024-dim vector.
8. **Store**: write to project's LanceDB table with all metadata.
9. **Done**. Chats in that project can now retrieve from it.

Chunk IDs are deterministic (`hash(source, heading, position)`) so re-indexing the same source produces the same IDs.

### Forced Chunks management

Two surfaces, both editing the same `rag_state.forced_chunks` state. Use whichever is more ergonomic for the operation at hand.

#### Surface 1: Forced Chunks popup (in-app, primary)

The main GM-facing UI for managing forced chunks. Lives inside Chorus, opens from a button in the chat view (or via a keyboard shortcut). Edits the active chat's `rag_state.forced_chunks` directly via a backend API; no LLM call, no token cost — pure CRUD on the chat JSON.

**Layout (top to bottom):**

**1. Header bar.**

- Project name + chat name (so the GM knows which chat they're editing)
- Live token count: "Forced context — Single: 12,300 | Events: 8,720 | Narration: 14,540" — updates as pins change. Warns if any column exceeds a configurable threshold (e.g., 30k).
- Save / Cancel buttons (or auto-save with explicit close)
- "Save current as project default" → writes the current set to `projects/<id>/rag_config.json` (with a confirmation step)

**2. Search and filter row.**

A sophisticated search bar with multiple modes layered together:

- **Free text** — fuzzy match against chunk `heading`, `parent`, `content`, `names`, and `tags` simultaneously. Substring match plus light tokenization. Handles "cragmaw" matching "Cragmaw Hideout."
- **Filter chips** (multi-select):
  - Content kind: mechanical / narrative / mixed
  - Source: lmop / dead_air / broken_orbit / etc.
  - Parent: dropdown of unique `parent` values from the corpus (e.g., "Cragmaw Hideout", "Phandalin", "Wave Echo Cave")
  - Tags: multi-select from corpus tag set
  - GM-only: yes / no / either
- **State filters**:
  - "Currently pinned (any agent)" — show only entries with at least one non-zero counter
  - "Starred" — show only starred chunks (chat or global)
  - "Globally starred" — show only globally-starred chunks
  - "Recently retrieved" — show chunks that appeared in the last N turns' retrieval log even if not currently pinned
- **Sort**:
  - By heading (alphabetical)
  - By most recently pinned
  - By most recently retrieved
  - By token size (descending — useful for spotting high-cost pins)

Search and filters compose. Result count visible.

**3. Starred section (always-visible, top of table).**

Starred chunks live in a permanent section at the top of the table, *always shown regardless of search and filters*. This is the "frequently used" surface for campaign-essential chunks.

**Three star states:**

| State | Visual | Meaning |
|---|---|---|
| Off | ☆ (outline, gray) | Not surfaced anywhere |
| **Chat-starred** | ⭐ (yellow) | Surfaced at the top of the popup in *this chat only* |
| **Globally starred** | ⭐ⓖ (yellow with G overlay) | Surfaced at the top of the popup in *every chat in this project*, including ones that don't have it pinned |

Globally starred subsumes chat-starred — there's no "both" state because global already implies the chat is one of the chats covered. When a chunk is promoted to global, the underlying chat-star flag (if any) is auto-cleared as redundant.

**Click semantics:**

| From | Single click | Double click |
|---|---|---|
| ☆ off | ⭐ chat | ⭐ⓖ global |
| ⭐ chat | ☆ off | ⭐ⓖ global (promote) |
| ⭐ⓖ global | ☆ off | ⭐ chat (demote) |

Two simple rules:
- **Single click** = on/off toggle. From any starred state goes to off; from off goes to chat (the default starred level).
- **Double click** = chat ↔ global toggle. From off jumps straight to global; from chat promotes to global; from global demotes to chat.

Every state has both gestures doing something useful. Tooltip on the star icon: "Click: star/unstar for this chat. Double-click: toggle global star (project-wide)."

**Storage:**

- **Chat star** lives on the chat-level forced_chunks entry as `starred: true|false`.
- **Global star** lives in the project's `rag_config.json` as a list of chunk IDs in `global_starred_chunks`. See Layer 4.

**Globally-starred chunks appear at the top of the popup in every chat in the project, even chats that don't have the chunk in their forced_chunks list at all.** Star and pin are orthogonal: starring controls visibility/sort priority in the popup; pinning controls runtime context inclusion via the per-stage counters.

**4. Main table.**

| Star | Chunk | Kind | Source / Parent | Tokens | Single | Events | Narration | Actions |
|---|---|---|---|---|---|---|---|---|
| ⭐ⓖ | **Sildar Hallwinter** | narrative | lmop / Phandalin NPCs | 412 | -1 | -1 | -1 | ✂ |
| ⭐ | Room 7: Goblin Den | mechanical | lmop / Cragmaw Hideout | 287 | 5 | 5 | 0 | ✂ |
| ☆ | Klarg the Bugbear | mixed | lmop / Cragmaw Hideout | 643 | 0 | 0 | 0 | ✂ |

Column behavior:

- **Star**: three-state control per the table above. Single/double click semantics as specified.
- **Chunk**: the heading, **clickable**. Clicking opens the preview side panel showing the chunk's full `content` + `contextual_preamble` + metadata (source, parent, tags, names, token count, agent eligibility). Critical because headings alone often aren't enough to know if it's the right chunk. Hover shows the full path as a tooltip without opening the preview.
- **Kind**: badge — color-coded mechanical/narrative/mixed.
- **Source / Parent**: light-text crumb showing where this chunk lives in its corpus.
- **Tokens**: chunk's `token_count`. Helps the GM see what they're spending on each pin.
- **Single / Events / Narration**: editable cells. Each cell shows the current counter. Click to edit.
- **Actions**:
  - ✂ Clear — sets all three counters to 0 (unpins from everything; entry becomes dormant).

**Dormant entries** (all three counters at 0) are hidden by default. A "show inactive (N)" toggle above the table reveals them as greyed-out rows. Click any counter cell on a dormant entry to re-activate; the entry becomes live again with the new counter value. No need to re-search the corpus to re-pin a chunk you previously had pinned in this chat.

**5. Cell editing.**

Click a counter cell → input opens with quick-action buttons:

| -1 (∞) | 1 | 3 | 5 | 10 | 0 (clear) | [custom] |

Clicking `-1` sets permanent. Clicking `0` clears. Clicking a number sets that many turns. The custom field accepts any integer including `-1`.

Bulk-edit row: clicking the chunk row's empty area opens "set all three columns to..." prompt.

**6. Bulk action bar (above the table).**

- "Pin all visible to N turns for [agent]" — applies to filtered/searched results
- "Unpin all visible" — sets all visible counters to 0
- "Hide inactive" / "Show inactive" — toggle visibility of dormant entries (those with all counters at 0). Dormant entries are never deleted; toggle just controls whether they appear in the table.
- "Decrement all positive by 1" — manual turn advance, mostly for testing

**7. Recently-retrieved sidebar.**

A collapsible sidebar showing the chunks that were actually live in the last N turns of retrieval (read from the rag_log). For each entry: heading, last score, "+pin" button to add a quick pin without searching for the chunk.

Use case: mid-session, the system retrieves a chunk that you realize is going to keep being relevant for the next few turns. Open the popup, see it in the sidebar, click +pin, set 3 turns, close. Done in seconds.

**8. Footer / status.**

- "X chunks pinned (Y permanent, Z expiring)"
- "Last edited: [timestamp]"
- Save/Cancel/Close

#### Surface 2: Claude Code workflow (scripting/bulk)

For operations the popup is awkward for, or when you want to script setup:

- Bulk re-stickying after a corpus re-index (chunk_ids changed)
- Setting up a new chat from a template (e.g., "copy the same starred set from chat A to chat B")
- Editing project-wide defaults via direct text-file editing of `rag_config.json`
- Querying the corpus by complex predicates Python lets you express that the popup search doesn't
- Programmatic setup ("for each NPC in the active arc, pin permanently to events+narration")

Workflow (same as before):

1. GM tells Claude Code what to do.
2. Claude Code runs corpus query script against LanceDB.
3. Returns candidates with headings and metadata.
4. GM confirms/edits.
5. Claude Code writes to chat JSON's `rag_state.forced_chunks` (or `rag_config.json`).

Possible Claude Code skill: `/find-chunks <query>` and `/sticky-chunks <chat_id>` wrapping these scripts. Build if/when manual invocation gets repetitive.

**The two surfaces never conflict because they edit the same underlying state.** The popup is the daily-driver; Claude Code is the power-user escape hatch.

### Tuning skill (post-session)

Hysteresis thresholds (`T_in`, `T_out`, consecutive-turns-N) cannot be picked intelligently upfront. They emerge from real sessions.

After a session, Claude Code skill:

1. Reads the chat's `rag_log.jsonl`.
2. Walks through turns, surfacing any where retrieval looked wrong (chunk should have been live but wasn't, or was live but shouldn't have been) for GM tagging.
3. Builds a small ground-truth set from the tags.
4. Sweeps `T_in` / `T_out` / `consecutive_below_t_out` parameter combinations, reports which match the GM's judgment best.
5. Suggests updated thresholds; GM accepts or overrides.

Even simpler v0 before this skill exists: just dump per-turn scores as CSV, eyeball it, manually nudge thresholds. Build the skill once you've felt the pain of doing it manually a few times.

### Agent-driven testing (synthetic, pre-tuning)

Before committing real play sessions to the system, use Claude Code agents to drive synthetic test scenarios. This catches gross bugs cheaply and lets us iterate on retrieval logic without burning real campaign time or real Opus tokens.

**Three versions, different cost/fidelity profiles. Use Version 1 for almost everything; Version 3 sparingly for end-to-end validation; skip Version 2.**

**Version 1: Retrieval-only tests (free, fast, primary tool).**

No real chat, no Opus calls. A spawned agent generates a synthetic player turn sequence (~10–20 turns) for a specific scenario. The harness then runs the actual RAG retrieval logic against that sequence in isolation:

1. For each turn, build the query from the rolling window of synthetic turns
2. Run hybrid search against the real LanceDB corpus
3. Apply hysteresis against the previous turn's live set
4. Output what `live_chunks` would look like after that turn
5. Write to a synthetic rag_log

Result: a complete rag_log for a fake session in seconds, with **zero API token cost** because nothing is actually generating responses. We immediately inspect it to ask: did the right NPCs get retrieved when mentioned? Did chunks evict cleanly when the scene transitioned? Did the rules subsystem come in at the right moment?

This tests the *retrieval logic specifically* — the part most likely to have tuning issues — and is cheap enough to run dozens of variants per minute. Tweak T_in, re-run. Tweak the chunker, re-index, re-run. Free, fast, focused. **This is where most synthetic tuning should happen.**

**Version 2: End-to-end with mocked responses (free, lower fidelity, skip).**

Two agents: one acts as the player, one acts as Opus. Tests the full integration loop including chunk rendering, but the "Opus responses" don't match real Opus's behavior (different model, different context discipline). Catches gross integration bugs without spending tokens, but the fidelity loss isn't worth it when Version 3 is already cheap. Skip.

**Version 3: End-to-end with real Opus (cheap, highest fidelity, sparingly).**

Agent simulates a player against a real Chorus test chat that hits real Opus through the actual backend. Agent calls are within the Claude Code budget; Opus calls are real API tokens via Chorus.

At synthetic RP scale (10–20 turns), this is *very* cheap — on the order of $0.20–$1.00 per scenario depending on chunk volume. A few of these at the end of the bug-hunt phase confirm end-to-end behavior is solid before committing a real Dead Air session.

**Recommended workflow:**

1. **Iterate rapidly with Version 1** — find and fix retrieval bugs, sanity-check chunker output, run dozens of scenarios. All free.
2. **A few Version 3 runs** at the end of the bug phase to confirm end-to-end with real Opus. Cents per test.
3. **Skip Version 2** entirely.
4. **Then real play sessions** for actual tuning against authentic distributions (see Tuning skill above).

**Generating realistic synthetic scenarios:**

Agent-generated player turns won't perfectly match the GM's real prompt distribution. Mitigate by basing synthetic scenarios on **excerpts from real session transcripts**, not on agent-from-scratch generation:

- Pull a chunk of past play from a Dead Air or Broken Orbit transcript
- Hand it to the agent with instructions to extend the conversation in the same style for 10–20 more turns
- Use multiple short excerpts from different scene types (combat, social, exploration, planning, OOC) to cover the real distribution

Output is closer to real prompt shape than agent-from-scratch generation. Not perfect, but much closer.

**What synthetic testing covers:**

- Retrieval logic correctness (Version 1)
- Hysteresis behavior across scene transitions (Version 1)
- Chunker output quality (Version 1, by inspection)
- Cache rendering and re-homing edge cases (Version 3)
- End-to-end integration (Version 3)

**What synthetic testing does NOT cover:**

- Whether retrieved chunks *help the model produce better play* (need real sessions)
- T_in / T_out tuning to authentic prompt distributions (need real sessions)
- Subtle continuity/callback issues that emerge over many sessions (need real sessions)

Synthetic testing is for bugs and obvious failures. Real-session tuning is still required for the parameter values themselves.

**Test scenarios worth running first:**

- **Scene transition** — start in location A, several turns, deliberately move to location B. Verify A chunks stay live during transition, drop cleanly after.
- **Callback** — reference something from earlier in the test session in different wording. Verify the relevant chunk re-retrieves.
- **Multi-NPC scene** — conversation involving 3–4 NPCs at once. Verify all the right chunks live simultaneously.
- **OOC interjection mid-scene** — drop OOC, resume play. Verify live set doesn't churn weirdly.
- **Subsystem trigger** — trigger a hacking check (Dead Air) or ship combat (Broken Orbit). Verify the right rules chunks come in and irrelevant ones don't.

Use a throwaway test chat in the same project (so it sees the same corpus) and delete it when done. Don't run synthetic scenarios in real campaign chats.

---

## Cost Model

**One-time costs:**

- Indexing a typical module (~80–120k tokens): ~$0.02 for embeddings + a few dollars for Opus 4.6 chunking + a dollar or two for contextual preambles. Total under $10 per module, paid once.

**Per-turn runtime cost:**

- Embedding the query (~1500 tokens): ~$0.0003. Negligible.
- LanceDB query: free (local).
- **No LLM call for retrieval.** The full RAG layer adds essentially zero per-turn cost.

**Cache savings (the actual point):**

- A 10k-token chunk that lives for N turns and is then evicted contributes 10k tokens to context for N turns and 0 thereafter.
- Compared to "load whole module upfront": potentially 100k+ tokens of permanent context replaced with ~10–30k tokens of dynamic context that comes and goes as needed. Per-turn input cost on Opus drops proportionally.
- Cache rebuilds (one per chunk eviction) cost a one-time pass through the rebuilt suffix. Worth it for big chunks; conservative eviction discipline matters.

---

## Build Phases

Rough sketch. Detailed phase planning TBD if/when we start building.

### Phase 1: Indexing pipeline (offline, no backend changes)

- LanceDB integration script.
- Voyage AI integration script.
- Source split via Opus 4.6 (manual claude.ai workflow + script for the chunking outputs).
- Structural chunker for mechanical content.
- Semantic chunker driver for narrative content (calls Opus 4.6).
- Contextual preamble generator (cheap model).
- End-to-end indexing of one test source (LMoP).

At end of Phase 1: a queryable LanceDB table exists for one project. No runtime integration yet.

### Phase 2: Backend retrieval and rendering (single-agent flow only)

- Backend reads `rag_state` from chat JSON; treats absence as no-op (backward compat).
- Per-turn retrieval: query construction, hybrid search, hysteresis, live set update.
- Render `forced_chunks` + `attached_chunks` into Opus's context at send-time.
- Re-homing logic at sawtooth roll-off boundaries.
- Retrieval log writes to sidecar JSONL.

At end of Phase 2: single-agent Anthropic chats in a project with a corpus get RAG. No pipeline integration yet, no Claude Code skills yet, no tuning loop yet.

### Phase 3: Forced Chunks management surfaces

**3a: Claude Code workflows (scripting/bulk).**

- Corpus query script (find chunks by text/heading/tags).
- Chat-state edit script (write `forced_chunks` to chat JSON).
- Project rag_config edit workflow.
- Optional: package as Claude Code skills (`/find-chunks`, `/sticky-chunks`).

**3b: Forced Chunks popup (in-app UI, primary surface).**

- Backend API: `GET/PUT /api/chats/<chat_id>/forced_chunks` (read/write `rag_state.forced_chunks`).
- Backend API: `GET /api/projects/<project_id>/corpus/search` (search/filter the LanceDB corpus from the frontend).
- Backend API: `GET /api/chats/<chat_id>/recent_retrievals` (read recent rag_log entries for the sidebar).
- Frontend popup component (React): table, search/filter, cell editing, starred section, recently-retrieved sidebar, live token counter, bulk actions.
- Star toggle persistence in the corpus or in chat-level state (TBD — see open questions).
- Auto-save on close, with explicit save/cancel option.
- Token-budget warnings when forced context for any agent exceeds threshold.

### Phase 4: Multi-agent pipeline integration

- Routing function `chunks_for_stage(stage_name)` covering `single`, `events`, `narration` — used by all flows.
- Standard 3-stage pipeline rendering: Events and Narration get their chunks, Mechanics is untouched (still uses static docs in LLM mode, deterministic Python in deterministic mode).
- 2-stage mode pipeline rendering: Planning uses the `events` column, Narration uses the `narration` column, Backend Resolution is untouched.
- Per-agent cache invariants (no new code, but verify behavior).
- Note: no special "Mechanics deterministic chunk routing" work — Mechanics doesn't consume RAG chunks in any form.

### Phase 5: Tuning loop

- Post-session analysis skill: read rag_log, surface candidate misses, build ground truth, sweep parameters, report.
- v0: CSV dump for manual eyeballing.

### Phase 6 (deferred, may never happen)

- Cross-reference graph layer (chunks linking to other chunks by reference).
- In-chat slash commands for forced chunks (alternative to Claude Code workflow).
- Per-agent retrieval queries (different weightings per agent).
- Reranking layer (cross-encoder rescoring of top-N candidates).
- Long-term memory / running summary system, if sticky chunks prove insufficient.

---

## Open Questions / Deferred Items

These were raised during design and explicitly left unresolved or deferred. Revisit when building.

1. **Cross-reference graph** (chunks linking to other chunks): deferred to a possible Phase 6. Can be added later without re-indexing because it's an additive layer on top of the chunk store. Not justified by undetectable-quality-regression argument because complexity has its own failure modes and the benefit is more situational than uniform.
2. **In-chat slash commands** for sticky/pin: deferred. Claude Code workflow handles it. Build only if alt-tabbing becomes annoying.
3. **Per-agent retrieval queries**: deferred. Single retrieval with per-agent filtering is sufficient unless a real pain point emerges.
4. **Reranking layer**: deferred. LanceDB supports it natively when we want it.
5. **Long-term memory / conversation summary**: not planned. Sticky chunks should handle the "I need this to survive the rolling window" need. Revisit only if real sessions show sticky list doesn't cover enough.
6. **Corpus versioning**: when re-chunking a source with better preambles or new logic, do existing chats get updated chunks or stay on old ones? Instinct: stay on old, new chats use new. Means tagging corpus tables with a version. Defer until needed.
7. **Multi-chunk re-homing collisions** (multiple chunks' hosts roll off in the same turn): no special handling needed; they all move into the latest message's `attached_chunks`. Just confirm bookkeeping when implementing.
8. **OOC explicit short-circuit**: defer. Implicit handling (vector search just doesn't match in-game chunks on OOC text) should be enough.
9. **Hysteresis initial values**: cannot be picked upfront. Start with rough guesses (e.g., T_in = 0.65, T_out = 0.45, N = 2), tune via the post-session skill against real sessions.
10. **Forced chunks popup token-budget threshold**: at what total per-stage forced size should the popup warn the GM? Probably configurable, default ~30k tokens. Worth re-evaluating after a few real sessions.
11. **dnd5e_cyber Mechanics migration**: BO currently uses LLM-based Mechanics with `mechanics_contract`. Plan is to convert to deterministic Python before next play. Out of scope for the RAG plan itself but worth tracking — once migrated, the "LLM Mechanics with static docs" path becomes vestigial and can be removed from this doc's Pipeline Interaction section.

**Resolved during design (kept for reference):**

- ~~Star persistence scope (per-chat vs global)~~ → **Resolved**: both. Three-state star (off / chat / global) with single-click toggling on/off and double-click toggling chat ↔ global. Chat star lives on the chat-level forced_chunks entry; global star lives in the project's `rag_config.json` `global_starred_chunks` list. See §Components / Forced Chunks and Operations / Forced Chunks management.

---

## Decision Log (key calls and why)

**Chosen:**

- **LanceDB** over sqlite-vec, Chroma, pgvector: native hybrid search, native reranking pathway, equal integration cost, better long-term ceiling.
- **voyage-3-large** over OpenAI text-embedding-3-small/large: best-in-class quality, cost negligible at our scale, undetectable regressions if we settled for lesser.
- **Two-stream chunking** (mechanical + narrative split via Opus 4.6): respects the dual nature of TTRPG source material; lets each stream use its ideal chunking strategy.
- **Multi-label `agents` field** instead of single `content_kind` routing: NPCs, items, factions cross agent boundaries; one-label routing was too coarse.
- **Contextual retrieval in v1**: ~35% retrieval improvement, modest one-time cost, not reversible without re-embedding, regressions undetectable if skipped.
- **Pure deterministic retrieval** (vector + BM25, no LLM in the loop): zero per-turn LLM cost, no hallucination surface, simpler architecture. Failure modes (premature eviction, missed conceptual links) are recoverable via sticky chunks and forced pins.
- **Multi-turn rolling-window query** capped at ~16k tokens: gives retrieval scene memory, avoids premature eviction when wording shifts mid-scene, respects voyage embedding context limit.
- **Single retrieval, route to agents by `chunk.agents`**: avoids 3× retrieval cost in multi-agent flow.
- **Forced chunks as one concept** (formerly two: sticky and pin) with **per-agent expiry counters**: each chunk has independent counters for `single`, `events`, `narration`. `-1` = permanent, `0` = unset/expired, positive N = turns remaining (decremented). Allows pinning the same chunk to different agents with different lifetimes.
- **Mechanics doesn't consume RAG chunks at all**: in deterministic mode (cpred), it's pure Python with no LLM call and no docs. In LLM mode (dnd5e_cyber until migrated), it's an LLM agent that uses its existing static doc assignments via the per-agent doc system, not RAG. In single-agent flow, mode tools (`combat_tool`, `hack_tool`, etc.) are deterministic Python and don't consume chunks. The popup has no Mechanics column because there is nothing for the GM to curate. The unifying rule: chunks route to LLM stages that need narrative context (Single, Events, Narration); Mechanics is mechanical and uses code or pre-existing static docs, never RAG.
- **Forced Chunks popup** as the primary management surface, with the Claude Code workflow as a coexisting power-user/scripting escape hatch: both edit the same `rag_state.forced_chunks` state. Popup handles daily-driver use; Claude Code handles bulk operations, scripted setup, and direct config-file editing.
- **Three-state starred chunks (off / chat / global)** with single-click on/off toggle and double-click chat ↔ global toggle: solves both per-chat starring (campaign-run specific) and project-wide starring (always-relevant for any chat in the project) without forcing one or the other. Globally-starred chunks surface in every chat in the project automatically. Visual: yellow star (chat) or yellow star with G overlay (global).
- **No automatic deletion of dormant forced_chunks entries**: when all per-stage counters reach 0, entries stay in the chat's `forced_chunks` list as dormant. The popup hides them by default with a "show inactive" toggle. Acts as a "recently pinned in this chat" history that can be re-activated with one click without re-searching the corpus. Nothing is ever deleted from LanceDB; only counter values change.
- **Two pipeline types supported uniformly**: standard 3-stage (Events → Mechanics → Narration) and 2-stage mode pipeline (Planning → Backend Resolution → Narration). The 2-stage Planning role is mechanically the same as Events, so it shares the `events` column. No separate `planning` column needed.
- **Messages reference chunks by ID** (not embedded content), rendered at send-time: clean eviction, no duplication, transparent inspection, deterministic caching.
- **Retrieval log in sidecar JSONL**, not in chat JSON: keeps chat files lean, supports tuning workflow without affecting gameplay performance.

**Deferred (not killed):**

- Cross-reference graph (additive, can come later).
- Reranking layer (LanceDB-native when needed).
- Per-agent retrieval queries (single + filter is sufficient first).
- In-chat slash commands for pinning.
- Long-term memory / running summary.

**Rejected:**

- LLM in the retrieval loop (Haiku-driven RAG): adds per-turn LLM cost, hallucination risk, latency. Pure vector + BM25 + sticky overrides covers the need.
- Tool-call-based retrieval by Opus: doubles Opus invocation per turn, hallucination risk on chunk IDs, GM-driven manual pin handles the same need.
- Conversation summarization: irreversible precision loss, sticky chunks handle the same need more transparently.
- Sliding window only: too dumb for reference material.
- Loading whole modules upfront: defeats the entire purpose; this whole system exists to avoid it.

---

## Don't build this until you need it

Real sessions will tell you what's worth building. Pre-optimization is the root of all evil. The plan exists so that when context bloat actually starts hurting, the design work is already done and we can move straight to implementation without re-deriving everything from scratch.

Run a few sessions of a published module (e.g., LMoP) using the simpler approach first — pre-split files, manual staging, the existing Chorus per-agent doc system. If that turns out to be enough, keep doing it. If it stops being enough, this doc is here.
