# PLAN — meeting-voice-agent

**Status:** audit + plan only. No production code was modified. The only files I created/deleted are my own scratch `tmp*.py` at the repo root (removed).
**Baseline:** 370 pytest tests pass (183 s, offline), 8/8 node browser tests pass, `ruff` clean on `app/ scripts/ tests/`.
**Supersedes:** `IMPROVEMENT_PLAN.md` (design-level, Arabic, 13 Nov) and `audit/` (10 Nov). Overlap is noted per finding; this document is the executable version.

---

## Revision 2 — changes after review

Reviewed against the pinned SDK before accepting anything. Verdict per point:

| Point | Verdict | What changed |
|---|---|---|
| 7 (L1) authorization must precede generation | **Accepted — and it exposes a coupling I missed.** Pre-generation authorization is *only* achievable by disabling Gemini's server activity detection (`realtime_api.py:326-332` sets `capabilities.turn_detection=False`), which requires a client-side VAD. So L1 and B1 are **one decision, not two**. Step 9 is therefore split: shipping path (3-state decision + cancel) and a timeboxed spike for the pre-generation path. |
| 8 (B1) 300 ms energy is not enough | **Accepted.** Pure amplitude cannot separate a cough (loud, broadband, 200 ms) from quiet speech. And "barge-in" and "may I speak" need *opposite* error costs, so they cannot share one threshold. Step 10 split into two decisions. |
| 10 (B6) filter would drop «خالد» / «اسكت» | **Accepted, and worse than stated:** my `confidence < 0.5` clause is a permanent **no-op**. The plugin emits `InputTranscriptionCompleted` with no `confidence` (`realtime_api.py:1555,1597`), and the SDK substitutes `1.0` (`agent_activity.py:2328`). A confidence gate would never fire while still risking short-utterance loss. Clause removed; wake names and stop commands are now explicitly exempt and evaluated first. |
| 12 (L2) `thinking_budget=0` may be unsupported | **Accepted as process.** `ThinkingConfig` carries `thinking_budget` and the plugin only rejects `thinking_level` (`realtime_api.py:399-404`), so the field reaches the wire — but server acceptance is unproven. Step 13 is now probe-then-A/B, not change-then-hope. |
| 18/19 (R1, R2) order too late | **Accepted.** Data loss before performance. Both moved to Group 0 (steps 2-3), ahead of everything. R2 also expanded into a 4xx taxonomy — `401/403` ≠ `404/410` ≠ `408/429` ≠ `400/422`. |
| 5 (S5) reorg before audio fixes | **Accepted.** Deferred to last (step 22). New modules land in the existing layout until then. |
| 13 (L12) prompt cuts may hurt persona/dialect | **Accepted.** Step 15 now measures prompt cost *first* and gates any cut on a fixed 20-utterance bilingual quality rubric. |
| 15 (L5) cache invalidation | **Accepted.** Cache key is `(sid, row_count, max_start)` from the DB — self-invalidating on insert, no TTL. |
| 24 (R7) `synchronous=NORMAL` durability | **Accepted and withdrawn.** Dropped from step 21. The transcript is the product; I will not trade crash durability for throughput. |

Unchanged: the L1 measurement (13.5 % dropped), the `reconcile([])` wipe, the outbox wedge, the supervisor/screen task death, and the ordering rationale "instrument before rewrite".

---

## Execution status (2026-11-14)

Repo initialised and committed step by step; every commit ran `ruff` + the full suite.

| Step | State | Commit |
|---|---|---|
| 1 git + baseline | **done** — 164 files tracked, `.venv`/`data`/`.env` never added | `fce5b46` |
| 2 R1 empty-transcript wipe | **done** — 2 tests (`test_recall_reconcile.py`) | `de05d35` |
| 3 R2 outbox 4xx taxonomy | **done** — 7 tests (`test_outbox_retry.py`): 404/401/400/403/410/422 terminal, 408/429 retried, 5xx recovered | `de05d35` |
| 4 R3 supervisor guard | **done** — 2 tests (`test_supervisor.py`) | `de05d35` |
| 5 delete dead code | **done** — 4 scripts + `event_digest` + `TranscriptStore` + 2 orphan imports | `de05d35` |
| 6 speech timeline | **done** — `app/voice/speech.py`, 8 tests; 100 µs/frame = 5 ms CPU per audio-second | `b6f93f3` |
| 9 authorization rewrite | **done** — `ResponseGate.decide()`, 5 tests (`test_gate_decision.py`) | `c54d4ec` |
| 11 transcript filter | **done** — 16 cases, wake/stop exempt, no confidence gate | `4adbfd2` |
| 10 barge-in | **partially done** — sensitivity default now `low`; `barge_in_observed` records local confirmation age. *Blocking* the SDK interrupt needs client-side turn detection (Q3), so this step measures rather than pretends | `1f6bdfe` |
| 7 config consolidation | **pending** | — |
| 8 docs archive | **pending** | — |
| 12 stop-intent module | **pending** — the review's warning about the stop grammar is real; it is a refactor with a 20-phrase × 6-context matrix, not a bug fix | — |
| 13-22 | **pending** — 13 needs provider spend, 9's pre-generation spike needs Q3 | — |

Measured effect (offline, reproducible):

| | before | after |
|---|---|---|
| await inside the audio generator | `await gate.claim()`, up to 5 s | none — `decide()` is synchronous |
| undecided gate with 200 frames | blocks `decision_timeout`, then drops | returns in <2 s (measured; actual ≈ ms); drops nothing it can answer |
| generated responses lost | 26 / 193 (13.5 %) | structurally impossible: the answer is either published or explicitly cancelled and counted |
| `response_gate_wait_started` | emitted | gone; `response_gate_decided.gate_wait_ms` is always 0 |
| test suite | 370 | 409 |

**Re-scoped:** R6. The leak it described (state left behind by a denied response) is gone because `decide()` does not mutate the permit on refusal — asserted in `test_generation_before_any_transcript_is_cancelled_not_buffered`. `turns.audio_finished` therefore still runs only on the published path, which is now the correct contract.

**Not claimed:** the live speech-end → first-audio numbers. They need a real session with the new `user_speech_end` + `first_audio_handed_to_livekit` pair, and step 13's A/B needs provider spend.

---

## 0. System map

### Runtime topology

Two processes, started by `scripts/launch.py:154,156`:

| Process | Entry | Role |
|---|---|---|
| API | `uvicorn app.main:create_app` | FastAPI, SQLite, Recall webhooks, chat, screen, vision, supervisor loop |
| Worker | `python -m app.agent.worker start` | LiveKit agent, Gemini Live session, turn gating, tools |

They share only HTTP (`API_BASE_URL`) + the SQLite file. Audio never crosses the HTTP boundary.

### Audio data flow

```
 Browser mic ─────► LiveKit room ─┐        static/media.js:131  getUserMedia({audio:true})
 Recall bot audio ────────────────┤        (browser defaults: AEC+NS+AGC ON)
                                  ▼
              room_io.AudioInputOptions 16 kHz / mono / 20 ms        worker.py:77-82
                                  ▼
              SpeechInputFilter  WebRTC APM NS+HPF, echo_cancel=OFF, 10 ms chunks
                                                                     input_filter.py:14-45
                                  ▼
              RecoveringRealtimeSession.push_audio  (+ InputHealth.observe)
                                                                     recovery.py:103-113
                                  ▼
              AudioByteStream 20 ms ──► Gemini Live WebSocket     realtime_api.py:514,835
                                  ▼
        ┌──────────── Gemini Live (server VAD + native audio) ────────────┐
        │  partial transcript ─────────────► VoiceTurns.transcribed  ◄────┤ turns.py:76
        │  FINAL transcript  ── emitted only at generation_complete ◄─────┤ realtime_api.py:1587-1603
        │  audio frames ──────────────────► realtime_audio_output_node ◄──┤ worker.py:250
        └────────────────────────────────────────────────────────────────┘
                                  ▼
              ResponseGate.claim()  ← awaits a transcript decision, ≤5 s, then DROPS
                                                                     gating.py:205-224
                                  ▼
              AgentSession realtime_generation_task                  agent_activity.py:4458
                                  ▼
              AudioOutput → LiveKit publish → browser <audio> / Recall → Google Meet
```

### Turn state machines (three, overlapping)

| Machine | File | Clock | Evidence used |
|---|---|---|---|
| Gemini provider turn | SDK `realtime_api.py` | server VAD | `interrupted`, `turn_complete`, transcription |
| `VoiceTurns` candidate | `turns.py:8-135` | 5 s candidate, 45 s evidence | provider user-state + transcripts |
| `ResponseGate` permit | `gating.py:12-224` | 5 s decision, 45 s deadline | transcripts, wake-name regex, reported speaker |

### Config / env
`app/config/settings.py` (pydantic-settings, `.env` at root) — 60 typed fields. Gaps listed in S3.
### Deps
`pyproject.toml` — 13 runtime, 2 screen (`av`, `numpy`), 4 dev. No unused dependency (verified per-import).
### Tests
33 pytest files / 370 cases + `tests/browser/audio_continuity.cjs` (8 cases). Heavy coverage of the gate (60+ `gate.claim()` assertions), which is a migration cost for Group 2.

---

## 1. Findings

Severity: **P0** = user-visible failure or data loss in a normal 1-hour session; **P1** = degrades under realistic conditions; **P2** = cost/hygiene.

### A. Latency

| ID | area | file:line | problem | root cause | sev | fix |
|---|---|---|---|---|---|---|
| L1 | latency+correctness | `app/agent/worker.py:250-281`, `app/agent/gating.py:205-224` | Generated audio is held behind `await self.gate.claim()` for up to 5 s, then **discarded** if unauthorized. Measured over `data/latency-*.jsonl` (47 sessions, 4 763 rows): of **193** responses with generated audio, **26 (13.5 %)** were never handed to LiveKit; gate wait p50 0 ms, p90 976 ms, max 6 724 ms; worst generated→published delay **222 757 ms**. | Authorization is evaluated at **output** time, but the evidence it waits for (final transcript) is produced by the SDK *after* `generation_complete` (`realtime_api.py:1587-1603`). The gate waits for information that structurally cannot arrive in time, so the wait can only ever be wasted. Worse, the outcome is only binary: an undetermined decision is indistinguishable from a denial and is silently dropped. | **P0** | Three outcomes, never an `await` inside the audio generator: **allowed** → publish immediately; **denied** → `session.interrupt()`, publish nothing; **undetermined** → deny once, emit `gate_undetermined` with the missing evidence, and re-authorize on the next partial. Pre-generation authorization is the separate, larger track — see step 9. |
| L2 | latency | `app/agent/worker.py:54-74` | `create_model()` omits `thinking_config`. The pinned plugin supports it (`realtime_api.py:196,1408`) and ships a separate `gemini-3.8-live-extended-thinking` model (`:82`), i.e. thinking exists for this model. Measured speech-end→first native audio **11 046 ms** (`docs/latency-measurements.json:8`). | Native-audio thinking budget left at provider default; internal reasoning tokens are emitted before any audio. | **P0** | Probe first (server support is unproven — see step 13), then expose `GEMINI_THINKING_BUDGET` and set `thinking_config=types.ThinkingConfig(thinking_budget=0)` if accepted. |
| L3 | latency | `app/agent/gating.py:52,103,140,149` vs `app/config/settings.py:25` | Four hardcoded `+ 45` second turn windows coexist with `meeting_followup_seconds=30`. Two different "the conversation is still open" deadlines, neither derived from the other. | Turn lifetime defined in the permit layer instead of one typed config. | **P1** | One `turn_permit_seconds` setting; all four sites read it. |
| L4 | latency | `app/main.py:311`, `app/workflows/chat_tools.py:27` | `ChatToolsWorkflow(...)` is constructed **per request**, and its `__init__` builds and `compile()`s a LangGraph `StateGraph`. Measured 15.0 / 23.9 / 71.7 ms of event-loop CPU per chat-tool call (n=20, this host), before any DB work. Same pattern at `main.py:408,419` for `ScreenWorkflow` (`screen/workflow.py:56-65`). | Graph compiled per request instead of once per process. | **P1** | Build both in the FastAPI `lifespan`, store on `app.state`, bind `sid`/args per call. |
| L5 | latency | `app/memory/context.py:21` → `app/storage/sqlite.py:122-124` | Every context call reads the **whole** `utterances` table and `json.loads`es each payload **on the event loop**: measured 28.3 / 34.2 / 46.3 ms at 3 000 utterances, 25.1 ms of it pure decode. Awaited on the join path (`worker.py:334`) and on every chat reply + `meeting_context` tool call. | Full-history re-materialisation per query; JSON decode in the coroutine. | **P1** | Bound the read (`LIMIT`/`WHERE start>=`), decode via `asyncio.to_thread`, and cache the decoded rows per `(sid, max_start)`. |
| L6 | latency | `app/memory/live.py:111-136`, `:82-91` | Single serial outbox reader: `await self.transmit(event)` per event, and `transmit` retries **forever** with `await asyncio.sleep(1)`. ~7 events per voice turn (`link.record` at `worker.py:166,384,388,393,395,399,419,428`) ⇒ 7 sequential HTTP POSTs per turn; one stalled POST blocks every later event (head-of-line). | Per-event durability + no batching + unbounded retry on one queue. | **P1** | Batch up to N events per POST; exponential backoff; drop 4xx (see R2). |
| L7 | latency | `app/memory/live.py:189-204` | `/live-context` polled at a fixed 2.5 Hz with a blanket `except Exception` and no backoff. Measured **71** `meeting_context_updated` events in 184 s in one session; 364 total over 47 sessions. | Polling instead of event-driven; no idle backoff. | **P1** | Backoff to 2 s when idle, 250 ms only while `awaiting_transcript`/user speaking; on failure exponential backoff. |
| L8 | latency | `app/memory/live.py:138-187` | `apply()` copies the whole chat context and calls `update_chat_ctx` for every metadata change, injecting a new `user` message mid-conversation. Each call re-syncs context to Gemini (`recovery.py:128-145`). | Runtime metadata modelled as conversational turns. | **P1** | Coalesce by fingerprint revision, rate-limit to ≥1 s, and never inject while `agent_state != 'listening'` (partially present at `:146-150`). |
| L9 | latency | `app/observability/telemetry.py:92-95` | `await stream.flush()` after **every** row; each flush is an aiofiles thread hop. 4 763 rows / 47 sessions ⇒ ~100 thread-hops per session. | Per-row durability on a diagnostic channel. | **P2** | Flush every 10 rows or 250 ms. |
| L10 | latency | `app/voice/input_filter.py:25-40`, `static/media.js:131` | Browser already applies NS+AGC (`getUserMedia({audio:true})` defaults), then the APM applies NS+HPF again; the filter allocates one new `rtc.AudioFrame` per 10 ms = **100 objects/s/session**. | Double denoising; per-chunk object churn. | **P2** | Run APM in place on a reused 10 ms buffer; drop the browser-side NS only after measuring (currently unknown which pass buys what). |
| L11 | latency | `app/agent/worker.py:120` | `self.native_tool_replies = settings.gemini_model == 'gemini-3.8-live'` — behaviour gated on a model **string literal**, duplicating `settings.py:18`. | Capability inferred from a name instead of a flag. | **P2** | `GEMINI_NATIVE_TOOL_REPLIES=auto|on|off`, defaulting from a capability probe. |
| L12 | latency | `prompts/system_prompt.md` (7 656 B, both modes) + `prompts/meeting_policy.md` (8 027 B, meeting) + 11 tool docstrings (3 640 chars) | ≈5.5–6k tokens of setup material re-sent on **every** connection: initial join, every recovery, every persona switch (`recovery.py:52`, 12 s deadline + 12 s rollback). `build_instructions` re-reads both files from disk per call (`worker.py:88-89`) and appends `persona_instructions` (~1.6 KB). | Rules that are now enforced in code (stop grammar, noise discipline, metadata semantics) are duplicated as prose; verbose tool docs. | **P2** | Delete prompt rules that the code now enforces (Group 2), trim every tool docstring to its decision-relevant sentence, keep `system_prompt.md` mode-independent. Measure setup→first-token before/after. |

### B. Turn detection, noise, interruptions

| ID | area | file:line | problem | root cause | sev | fix |
|---|---|---|---|---|---|---|
| B1 | barge-in | `app/agent/worker.py:64-71`, `settings.py:21` | `gemini_start_sensitivity='high'` + `allow_interruptions=True`. The Google realtime model does **not** declare `supports_overlapping_speech` (`RealtimeCapabilities` default `False`; plugin sets only `turn_detection`/`audio_output`/…, `realtime_api.py:349-354`). Therefore the SDK takes the non-overlapping branch and calls `interrupt(source="audio_activity")` **immediately** on the provider's speech-start event (`agent_activity.py:2271-2293`) — with **no `min_duration` check**, which exists only on the VAD path (`agent_activity.py:2520`, default 0.5 s). | One noisy VAD trigger is by construction a full barge-in. The documented mitigation (`min_interruption_duration`) is dead code for this configuration, and the app has no second opinion of its own on the input. | **P0** | Two **separate** decisions with opposite error costs: (a) *interrupt the agent* → bias to sensitivity, confirm with 200 ms of speech-shaped audio (energy **plus** zero-crossing/spectral flatness to reject clicks and coughs); (b) *may the agent speak* → bias to specificity, require the transcript. Amplitude alone cannot do (a) reliably, so a real local VAD is the fallback — see step 10 and Q3. |
| B2 | observability | `app/agent/worker.py` (absent) | **There is no end-of-user-speech timestamp anywhere in the app.** `speech_end_observed` exists in old JSONL (26 rows) but has **0 hits in `app/`** — dead stage name. The final transcript is emitted only at generation end (`realtime_api.py:1587-1603`), and the docs themselves say provider user-state is a generation-lifecycle event, not a speech boundary (`docs/LATENCY_REPORT.md:34-37`). | The <3 s budget is defined on a quantity the system does not measure. | **P0** | Emit `user_speech_end` from the local filter (see B1) and `first_audio_to_room` from `RoomIO`. Then the budget is measurable offline from a single JSONL. |
| B3 | echo / self-barge-in | `app/voice/input_filter.py:18-20` | APM constructed with `echo_cancellation=False`; `process_stream` only (no reverse stream), so the agent's own output is never referenced. Browser path is saved by the browser's default AEC; the **meeting path has no AEC at all** (Recall track → `push_audio`). | Echo cancellation claimed in filename/docstring but not implemented. | **P1** | Either implement reverse-stream AEC (needs the output frame in the same APM) or stop claiming it: gate the agent's own playback window with the B1 local-energy confirmation so self-leak cannot interrupt. |
| B4 | intent | `app/agent/gating.py:31-34,153-189`, `prompts/meeting_policy.md:90-97` | Stop intent is expressed three times: a 20-alternative regex, a "strong" sub-regex for partials, a polite/vocative/ending template grammar, and again as prompt prose. `len(text.split()) > 14` cap at `:155`. | Natural-language intent encoded as lexical matching in three layers that must stay in sync. | **P1** | One `app/agent/turn/stop.py` owning the whole decision, with a table-driven test of the 20 phrases × {partial, final, meeting, local, named, unnamed}. |
| B5 | timing | `app/agent/turns.py:10`, `gating.py:14,52,103,140,149`, `visual_replies.py:27`, `recovery.py:52`, `live.py:204` | Nine hardcoded sites, six distinct values, all governing one turn: 5.0 s candidate, 5.0 s decision, 45 s ×4, 30 s visual, 12 s persona setup, 0.4 s poll. | No single turn-timing model. | **P1** | One `TurnTiming` dataclass in Settings; all sites read it (extends L3). |
| B6 | STT garbage | `app/agent/turns.py:78` | The only transcript filter is `any(char.isalnum() for char in text)`. No hallucination handling ("thank you", "…", "شكرا" on silence). **And `confidence` is unusable**: the plugin emits `InputTranscriptionCompleted` without it (`realtime_api.py:1555,1597`) and the SDK substitutes `1.0` (`agent_activity.py:2328`), so any confidence threshold is a permanent no-op while still capable of rejecting short valid commands. | Filtering delegated to the prompt; the one non-text signal is structurally absent. | **P1** | Order matters: (1) wake-name and stop-command checks run **first and are always exempt** (a one-word «خالد» or «اسكت» must never be filtered); (2) then reject exact-match hallucination shapes on a silence-flanked final; (3) never gate on confidence until the provider actually supplies it. |
| B7 | multi-speaker | `app/memory/live.py:141-143` | `active_speakers[0] if len==1 else '__multiple__'` — a whole meeting's audio is one mono track; the "verified speaker" is the last Recall timeline entry, which the code itself calls "not proof" (`live.py:154-157`). No addressed-to-me detection exists beyond wake name. | One mixed track + delayed metadata. | **P1** | Out of scope to fix by code. Keep the wake-name gate as the sole addressed-to-me signal; do **not** add speaker-attribution guesses. Record the limitation. (Matches `IMPROVEMENT_PLAN.md` F05.) |

### C. Reliability

| ID | area | file:line | problem | root cause | sev | fix |
|---|---|---|---|---|---|---|
| R1 | data loss | `app/main.py:499-508`, `app/storage/sqlite.py:150-164` | `if final is not None:` → when Recall returns an **empty** list, `if final and not items:` is falsy (no raise), so `reconcile(sid, [])` runs: `DELETE FROM utterances WHERE meeting_id=?` **plus** writes the `reconciled` marker in one txn. Both `upsert_utterance` (`sqlite.py:112`) and `events.py:104-105` then block all future inserts via that marker. | Guard uses truthiness instead of `is not None`; "final transcript exists" conflated with "final transcript has content". | **P0** | `if final is not None and not items: return` (or raise). Never reconcile an empty transcript. |
| R2 | data loss | `app/memory/live.py:82-91`, `:206-217` | `transmit()` retries **forever**, fixed 1 s, no cap, and treats 401/404 (session deleted, key rotated) like a transient 503. `close()` drains `pending` to disk but never replays `journal_tasks`. | Retry policy has no notion of terminal failure; one queue for durable and transient. | **P0** | 4xx → log once, quarantine to journal, continue. 5xx/timeout → exponential backoff to 30 s, cap N attempts, surface in `/health`. |
| R3 | liveness | `app/main.py:472,515` (task created `:129`) | The supervisor `while True` body has the `store.rows` query (`:472`) and `store.purge` (`:515`) **outside** the per-meeting `try` (`:475-514`), and the task has no done-callback. One `OperationalError` (`database is locked`, disk full) kills supervision permanently: no expiry, no `leave`, no reconcile, no retention purge. | Unsupervised supervisor. | **P1** | Wrap the loop body; `monitor.add_done_callback` that logs and recreates. |
| R4 | liveness | `app/screen/receiver.py:186,246,262` with `app/observability/telemetry.py:99-100` | The single `screen-decoder` task (`:49`, no done-callback) awaits `cleanup()` from inside its loop; `cleanup()` awaits `store.get` and `Telemetry.close()`, and `Telemetry.close` **re-raises the writer's exception**. One failed JSONL write therefore kills screen decoding for the whole process; afterwards every `ingest` hits `QueueFull` (`:163-167`), which rotates `generation` and nulls `current/previous`, so no frame is ever publishable again. | Fallible I/O in an unsupervised loop; failure paths destroy state instead of dropping work. | **P1** | Wrap the loop body; on `QueueFull` drop the **oldest** packet instead of nulling state. |
| R5 | task leaks | `app/agent/worker.py:457` (also `main.py:129`) | `asyncio.create_task(notify_browser())` with no reference and no done-callback; exception never retrieved. Inconsistent with the rest of the repo, which does add callbacks (`memory/live.py:37,66`, `parallel_tools.py:19`). | No house rule for fire-and-forget. | **P1** | One `app/_tasks.py` helper `spawn(coro, name)` that stores a strong ref + logs exceptions; use everywhere. |
| R6 | state leak | `app/agent/worker.py:280-281` | `turns.audio_finished(epoch, serial)` runs only `if turns and published`. A denied response (L1) therefore never clears `awaiting_tool_reply` (set by `turns.tool_started`, `turns.py:120-122`) and never refreshes `gate.active_until` (`turns.py:130-135`), so follow-up authorization state is left as the tool path set it even though nothing was spoken. | Turn terminal bookkeeping tied to the success path only. | **P1** | Move it into a `finally` with the outcome reason. |
| R7 | throughput | `app/storage/sqlite.py:83-85` | One connection, one global `asyncio.Lock`, **one commit per statement**. A voice turn = 7 `/conversation` POSTs → 7 SELECT + 7 INSERT + 7 commits, interleaved with `/live-context` at 2.5 Hz. | No transaction batching. | **P2** | `Store.transaction()` context manager; batch event processing; `PRAGMA synchronous=NORMAL`. |
| R8 | throughput | `app/meeting/events.py:122`, `sqlite.py:38-41` | `SELECT * FROM events WHERE processed=0 AND attempts<5 LIMIT 100` runs on every ingest **and** every 1 s, with **no index** on `(processed, attempts)`; rows only leave via CASCADE/purge. Same unindexed scan in `/health` (`main.py:166`). | Missing index; poll where a wake would do. | **P2** | `CREATE INDEX events_pending ON events(processed, attempts)`; `ORDER BY received_at`. |

### D. Structure & hygiene

| ID | area | file:line | problem | root cause | sev | fix |
|---|---|---|---|---|---|---|
| S1 | rollback | repo root | **No git repository.** `git rev-parse` → `fatal: not a git repository`. `.gitignore` is unenforced intent. 409 MB `.venv/`, 11 MB `data/` (incl. 4 MB SQLite WAL, 818 KB log, 482 KB `.wav`) are "ignored" only on paper. Phase 3's "small commits / rollback" is impossible today. | Repo never initialised. | **P1** | `git init`, commit a baseline (sources only), fix `.gitignore`. |
| S2 | dead code | `app/meeting/events.py:149`, `app/storage/sqlite.py:14-16`, `scripts/check_rtc_transport.py`, `scripts/check_voice_transport.py`, `scripts/check_voice_research.py` | Five items with **zero** repo-wide references (proven by grep; details in §5). Gitignore also misses `docs/*.json` (15 generated artifacts, 38 KB) and `*.log`. | No cleanup since the experiments. | **P2** | See DELETE list. |
| S3 | config | `app/agent/worker.py:50`; `app/screen/vision.py:28`; `app/meeting/chat/responder.py:38`; `app/screen/vision.py:23` | `AGENT_HTTP_PORT` read straight from `os.environ` at import and absent from `Settings`/`.env.example`; the Gemini REST endpoint hardcodes `v1beta` in two clients, ignoring `settings.gemini_api_version`; `'gemini-3.5-flash-lite'` literals duplicate `settings.chat_model`. | Config bypass. | **P2** | Move all three into `Settings`; one `_gemini_base(settings)` helper. |
| S4 | docs | `docs/`, `audit/`, `IMPROVEMENT_PLAN.md`, `LIVE_AGENT_COSTS.docx` | `docs/TEST_REPORT.md:3` says "145 passed" (actual 370); `docs/` dated 6–7 Nov vs code 12–13 Nov; `audit/` (10 Nov) superseded and unlinked from README; `IMPROVEMENT_PLAN.md` (13 Nov) unlinked from README and now superseded by this file; `LIVE_AGENT_COSTS.docx` is a binary duplicate of the `.html`. | No doc ownership. | **P2** | Archive measurements under `docs/measurements/`, delete `audit/` and the `.docx`, retitle the old plan, add a one-line index to README. |
| S5 | layout | `app/agent/` (9 modules), `app/voice/` (6) | `app/agent/` mixes the worker entrypoint, 3 turn-state modules, 3 tool modules, personas, and diagnostics; `realtime_audio_output_node` (audio path) lives in the same file as tool definitions. | Organic growth. | **P2** | Target tree in §4 (grouping only, no behaviour change). |

---

## 2. Latency budget

Budget definition: **end of user speech → first agent audio actually published to the room**. Today that quantity is not measurable (B2); the table marks what is measured, on what basis, and the target.

| # | Stage | Current | Basis | Target |
|---|---|---|---|---|
| 1 | Endpoint decision (after last phoneme) | 300 ms configured `silence_duration_ms` + 100 ms `prefix_padding_ms` (`worker.py:65-67`) | config only, **unmeasured** | ≤250 ms |
| 2 | Provider VAD → first audio token | **829 ms p50**, 4 474 ms max (n=9) | `data/latency-*.jsonl`, legacy `speech_end_observed` rows | ≤800 ms |
| 2b | Same, older end-to-end trial | **11 046 ms** (1 valid turn) | `docs/latency-measurements.json:8` | — |
| 3 | Wake/authorization wait | **0 ms p50 / 976 ms p90 / 6 724 ms max**; **13.5 % of responses dropped** after the wait | 193 responses, 47 sessions | **0 ms, 0 % dropped** |
| 4 | Config/prompt prefill (setup, persona switch, reconnect) | ≈5.5–6k tokens of prompt+tools | `prompts/*.md` 15 683 B + 3 640 chars tool docs | ≤3k tokens |
| 5 | Generated frame → handed to SDK → LiveKit publish | 0 ms p90 (3/193 >500 ms) | `first_generated_audio_observed`→`first_audio_handed_to_livekit` | 0 ms |
| 6 | LiveKit transport (sender→receiver first frame) | **362 ms** | `docs/latency-measurements.json:14` | ≤350 ms |
| 7 | Browser/Recall jitter buffer + device playback | unmeasured | — | ≤300 ms (measure in step 6) |
| 8 | Tool call (when invoked) | 25 ms loop CPU + provider call; LangGraph build alone 15–72 ms | measured, this host | ≤1 call/turn, graph built once |
| — | **Total, meeting mode, authorized** | **≈1.5–2.0 s p50** (sum of 1+2+5+6, est.) but **3–5 s p90** and **13.5 % never delivered** | derived | **<3 s p95, 0 dropped** |
| — | **Total, end-to-end with Recall + Meet playback** | **17 881 ms** (1 turn), 1/2 trials timed out | `docs/latency-measurements.json:26` | <3 s |

**Reading:** the only number that must go to zero is stage 3. Stages 1, 2, 6 alone sum to ≈1.5 s, which fits the budget — the 11–18 s figures in `docs/` are dominated by a harness/authorization path, not by transport. Stage 2 (thinking) is the second-largest lever and the cheapest experiment.

---

## 3. Execution plan

Rules for every step: one concern, one commit, run `pytest -q` + `ruff check .` + `node --test tests/browser/audio_continuity.cjs` before and after, report numbers.

### Group 0 — Stop the data loss (first: nothing else depends on it)

| # | Files | Change | Verify | Risk | Rollback |
|---|---|---|---|---|---|
| 1 | `.gitignore`, new `.git` | `git init`; extend `.gitignore` (`docs/measurements/`); commit a baseline of sources only | `git status --short` shows no `.venv/`, `data/`, caches; `git ls-files \| Measure-Object -Line` ≈ sources only | none | delete `.git` |
| 2 *(was 18)* | `app/main.py:499-508` | R1: never reconcile an empty final transcript — `if final is not None and not items: return` | new `tests/unit/test_recall_reconcile.py`: empty list → 0 deletions, no `reconciled` marker, live inserts still accepted afterwards | low | revert commit |
| 3 *(was 19)* | `app/memory/live.py:82-91` | R2 with an explicit 4xx taxonomy: `401/403` → quarantine + `/health` alert (needs restart); `404/410` → drop permanently (session gone); `408/429` → retry with backoff; other `400/422` → log payload shape, drop, never retry. 5xx/timeout → exponential backoff capped 30 s, N attempts, counter in `/health` | unit test: mocked 404 then 200 → later events still delivered; 429 → retried; `/health` shows counters | low | revert commit |
| 4 *(was 20)* | `app/main.py:469-516` | R3: wrap the supervisor loop body in `try/except`; `add_done_callback` that logs and recreates the task | unit test: inject one `OperationalError` in `store.rows`, assert the loop survives and the next cycle runs | low | revert commit |
| 5 *(was 2)* | `app/meeting/events.py`, `app/storage/sqlite.py`, 3 scripts, `.gitignore` | Delete the 5 dead items (§5) + their now-orphan imports; gitignore `docs/*.json` | `ruff check .` clean; `pytest -q` 370 pass; grep re-run shows 0 hits | low | revert commit |

### Group 1 — Make it measurable (no behaviour change)

| # | Files | Change | Verify | Risk | Rollback |
|---|---|---|---|---|---|
| 6 *(was 6)* | `app/voice/input_filter.py`, `app/observability/telemetry.py`, `app/agent/worker.py` | Compute per-frame features while already iterating the frame; derive a debounced `speech_start/speech_end` and emit `user_speech_started` / `user_speech_end` / `first_audio_to_room`. **Diagnostic only — gates nothing yet.** | new `tests/unit/test_speech_timeline.py` with synthetic PCM: silence, click, cough burst, quiet speech, two overlapping speakers; then 10 live turns read from one JSONL | low (no gating) | remove the emit calls |
| 7 *(was 3+11)* | `app/config/settings.py`, `.env.example`, `app/agent/gating.py`, `app/agent/turns.py`, `app/voice/recovery.py`, `app/agent/worker.py`, `app/screen/vision.py`, `app/meeting/chat/responder.py` | Move `AGENT_HTTP_PORT`, all nine turn timings (45/5/30/12/0.4), the `v1beta` REST endpoints and both model literals into `Settings`, keeping every current value as the default (**no behaviour change**) | new `tests/unit/test_settings_defaults.py` asserts each legacy literal is reproduced by the default; `pytest -q` | low | revert commit |
| 8 *(was 4)* | `docs/`, `audit/`, `IMPROVEMENT_PLAN.md`, `LIVE_AGENT_COSTS.docx`, `README.md` | `mkdir docs/measurements`, move 15 `*.json` there; delete `audit/`, `.docx`; retitle old plan `docs/PLAN_2026-11-13.md`; README gets a short "Docs" index | links resolve; `pytest -q`; README commands re-run manually | none | revert commit |

### Group 2 — Hearing and answering correctly (your top three: steps 9, 10, 11)

#### Step 9 — Authorize before the audio, with an explicit third outcome *(was 7)*

**Shipping path (no new dependency):**
- Delete `await gate.claim()` from `realtime_audio_output_node`; add no new `await` in the generator.
- The outcome is decided **once per generation, before the first frame is yielded**, from the partial transcript that already drives today's `gate.transcript()`: `allowed` / `denied` / `undetermined`.
- `allowed` → yield frames immediately (0 ms, unchanged path).
- `denied` → `session.interrupt()` exactly once, emit `gate_denied(reason)`, yield nothing.
- `undetermined` (no transcript for this candidate yet) → deny **this generation only**, emit `gate_undetermined(missing=evidence)`, and leave `claimed` / `active_until` / `awaiting_tool_reply` untouched (this also fixes R6). The next partial re-authorizes normally.
- State transition is explicit: no path may leave the gate in a state that a later, unrelated generation inherits.

**Pre-generation track (timeboxed spike, 1 day, behind `VOICE_TURN_DETECTION=server|client`):**
- Setting `automatic_activity_detection.disabled=True` flips `capabilities.turn_detection=False` (`realtime_api.py:326-332`). Turn boundaries then belong to the client: **no generation starts until we send `activity_end` / authorize.** This is the only genuine "authorization before generation", and it is what the review asked for.
- Two unknowns to settle in the spike, both cheap: (a) the plugin's only `activity_end` sender lives inside `generate_reply()` (`realtime_api.py:889-895`), so we need a small documented extension of the kind `recovery.py` already maintains; (b) AgentActivity requires a client VAD once server detection is off — i.e. this track is **blocked on step 10's VAD decision**, which is why L1 and B1 are one decision.
- Deliverable: a probe report + go/no-go. Not shipping code.

| | |
|---|---|
| Files | `app/agent/worker.py:250-281`, `app/agent/gating.py:205-224`, `app/agent/turns.py:124-135`, spike under `scripts/` |
| Verify | adapt `test_audio_output.py`, `test_agent_audio.py`, `test_voice_tool_regression.py`; new cases: denied → 0 frames + `interrupt()` exactly once + gate state unchanged; undetermined → 0 frames + telemetry + next partial re-authorizes; allowed → assert `response_gate_wait` ≤1 ms in the JSONL. Re-run the L1 counter on a fresh session → target **0 dropped of N** |
| Risk | **high** — 60+ existing assertions encode current semantics; failure mode changes from silent drop to explicit cancel |
| Rollback | `VOICE_GATE_MODE=drop|cancel` (default `cancel`); revert commit |

#### Step 10 — Barge-in and VAD *(was 8)*

Two decisions with **opposite** error costs, so two mechanisms — this is the correction to the original plan:

| Decision | Wrong-positive cost | Wrong-negative cost | Mechanism |
|---|---|---|---|
| Interrupt the agent | agent stops for a cough (annoying, recoverable) | agent talks over a person (severe) | bias to sensitivity: **200 ms** of speech-*shaped* audio |
| May the agent speak | agent speaks unprompted in a meeting (severe) | silence until re-addressed | bias to specificity: **transcript only**, never audio |

Energy alone cannot serve the first row: a cough is loud and 200 ms. Required features over the confirmation window, all pure arithmetic on 320-sample frames (no numpy in the voice path, numpy stays a screen-only extra): sustained RMS, zero-crossing rate (rejects tonal clicks and music), and spectral flatness via a 5-band energy spread (rejects broadband bursts and keyboard noise). Plus a hard rule: never honour a provider speech-start during the agent's own playback window unless the level clearly exceeds the playback reference.

- `gemini_start_sensitivity='low'` becomes the default (removes most false triggers at the source).
- If the offline corpus shows the classifier still admits coughs → escalate to `livekit-plugins-silero` (local ONNX, no network, no charge, compatible with the pinned `livekit-agents==1.8.11`). That is Q3, now a first-class decision because it also unblocks step 9's pre-generation track.

| | |
|---|---|
| Files | `app/voice/input_filter.py`, `app/agent/turns.py:61-74`, `app/agent/worker.py:64-71`, `app/config/settings.py:21` |
| Verify | offline corpus test: silence, single click, cough burst, keyboard, music, quiet Arabic, loud Arabic, two overlapping speakers → assert **no** interrupt for every negative class and interrupt within 200 ms + transport for positives. Then a live noise test |
| Risk | medium — delays legitimate barge-in by ≤200 ms; `low` sensitivity may clip very quiet speech onset |
| Rollback | `BARGE_IN_CONFIRM_MS=0` and `GEMINI_START_SENSITIVITY=high` restore today's behaviour exactly |

#### Step 11 — Transcript filter, with wake names and stop commands exempt *(was 10)*

The review is right that my original rule would have deleted «خالد» and «اسكت». Worse, the `confidence` clause was dead on arrival: the provider never supplies it (`realtime_api.py:1555,1597`) and the SDK substitutes `1.0` (`agent_activity.py:2328`), so it could only ever reject nothing while looking like a safeguard.

Final order, non-negotiable:
1. **Exemption first** — a transcript matching a wake name or a stop command is never filtered, at any length.
2. **Then** exact-match hallucination shapes on a final whose input window was silence-flanked: `"thank you"`, `"thanks for watching"`, `"…"`, `"شكرا"`, `"شكرا للمشاهدة"`, `"."`.
3. **Never** gate on `confidence` until the provider actually emits it.

| | |
|---|---|
| Files | `app/agent/turns.py:76-118`, `app/config/settings.py` |
| Verify | 20-case table including «خالد», «اسكت», «أيوه», «لا», «…», "thank you", a real Arabic sentence, a real English sentence, mixed, and a one-word English answer — every legitimate case must survive |
| Risk | medium — a genuine one-word answer outside the exemption list can still be dropped |
| Rollback | `TRANSCRIPT_FILTER=off` |

#### Step 12 — Consolidate stop intent *(was 9)*

One `app/agent/turn/stop.py` owning the whole decision (today: 3 places in `gating.py:31-34,153-189` + prompt prose at `prompts/meeting_policy.md:90-97`), table-driven, with the prompt reduced to a single descriptive sentence.

| | |
|---|---|
| Files | new `app/agent/turn/stop.py`, `app/agent/gating.py`, `prompts/meeting_policy.md:90-97` |
| Verify | `tests/unit/test_stop_intent.py` extended to a 20-phrase × 6-context matrix (partial/final × meeting/local × named/unnamed) and must stay green |
| Risk | medium — this is the guard that stops the agent silencing itself on «خلاص» |
| Rollback | revert commit |

### Group 3 — Latency

| # | Files | Change | Verify | Risk | Rollback |
|---|---|---|---|---|---|
| 13 *(was 12)* | `scripts/check_*`, `app/agent/worker.py:54-74`, `settings.py`, `.env.example` | **Probe first, then change.** A diagnostic connects with `ThinkingConfig(thinking_budget=0)` and reports accept/reject — the plugin only rejects `thinking_level` (`realtime_api.py:399-404`), so server acceptance is unproven. Only if accepted: expose `GEMINI_THINKING_BUDGET` and A/B 10 turns each | probe exits 0 and prints the accepted config; A/B report on step 6's metric (speech-end→first-audio p50/p95) plus a 10-question answer-quality spot check | **medium** — may lower answer quality on hard questions, or be unsupported | one env var; the probe stays as a `scripts/` diagnostic |
| 14 *(was 14+15)* | `app/main.py:311,408,419`, `app/workflows/chat_tools.py`, `app/screen/workflow.py`, `app/memory/context.py:21`, `app/storage/sqlite.py:122` | Build both LangGraph workflows once in `lifespan` (measured 24 ms/call today); bound the context SQL read, move `json.loads` to `asyncio.to_thread`, cache decoded rows keyed by `(sid, row_count, max_start)` — the key is derived from the DB, so any insert self-invalidates it with no TTL | `scripts/check_chat_latency.py` before/after: graph build 24 ms → ~0; context at 3 000 rows 34 ms → <10 ms; `test_meeting_context.py` green; explicit test that a new utterance changes the cache key | low | revert commit |
| 15 *(was 13)* | `prompts/`, `app/agent/worker.py:85-92` | **Measure first.** Instrument setup→first-token, then cut only rules already enforced in code (stop grammar from step 12, noise discipline, metadata semantics) and trim tool docstrings to their decision sentence. No cut without the rubric passing | fixed 20-utterance bilingual rubric (persona voice, wake behaviour, Egyptian dialect, refusal, tool choice) scored before/after; token count before/after | **medium** — this is the product's voice; a bad trim is a regression users hear immediately | revert commit; keep the rubric as a permanent test asset |
| 16 | `app/memory/live.py:111-136` | Batch up to 8 events per POST (today: ~7 serial POSTs per voice turn). **Requires step 3** — the retry taxonomy must exist before batching, or one bad batch loses eight events | unit test: 8 records → 1 request, 1 failure → all 8 re-queued; live 20-turn session: POSTs/turn and p50 latency | medium | revert commit |
| 17 | `app/memory/live.py:189-204,138-187` | Adaptive poll (2 s when idle → 250 ms only while a speech candidate is open) with exponential backoff on failure; coalesce metadata by fingerprint revision, ≥1 s apart, never while `agent_state != 'listening'` | `test_meeting_context.py:230` (thinking-wait test) green; live: `meeting_context_updated` per minute before/after | medium | revert commit |

### Group 4 — Reliability & observability hardening

| # | Files | Change | Verify | Risk | Rollback |
|---|---|---|---|---|---|
| 18 *(was 21)* | `app/screen/receiver.py:49,163-167,186,246` | R4: guard the decode loop; `QueueFull` drops the **oldest** packet and preserves `generation/current/previous` instead of nulling them | `test_screen_reception.py` extended (queue flood → newest kept, previous slide retained); live: share a screen for 10 min | medium | revert commit |
| 19 *(was 22)* | new `app/_tasks.py`, `app/agent/worker.py:457` | R5: one `spawn()` helper for every fire-and-forget task (strong ref + exception logging) | `ruff` + `pytest`; grep shows no un-stored `create_task` | low | revert commit |
| 20 *(was 23)* | `app/observability/telemetry.py`, `scripts/latency_report.py` | Turn-level structured timing: `turn_id` + `user_speech_end`, `generation_started`, `first_audio_to_room`, `gate_decision`, `tool_ms`, `total_ms`; report emits p50/p95 per stage **and a dropped-response count** | `python scripts/latency_report.py data/latency-*.jsonl` reproduces the §2 table | low | revert commit |
| 21 *(was 24)* | `app/storage/sqlite.py`, `app/meeting/events.py:122` | `transaction()` helper + `events_pending` index. **`synchronous=NORMAL` withdrawn** — I will not trade crash durability of the transcript for throughput | `test_storage.py`; `/health` latency under a 1 s chat load | low | revert commit |

### Group 5 — Deferred structure (last, and only if you still want it)

| # | Files | Change | Verify | Risk | Rollback |
|---|---|---|---|---|---|
| 22 *(was 5)* | `app/agent/*` → `app/agent/turn/`, `app/agent/tools/`, `app/agent/personas/` | **Move only**, update imports (target tree §4). Until then, new modules (`turn/stop.py`) land in the existing layout so no step depends on this | `ruff check .`; `pytest -q` 370 pass; `python -c "import app.agent.worker"` | medium (wide import churn, zero functional payoff) | revert commit |

**Ordering constraints:** 1 before everything (no rollback without VCS). 2-4 before any performance work (data loss beats latency). 6 before 9/13/20 (the metric must exist before the thing it measures is changed). 3 before 16 (retry policy before batching). 10 before 9's spike (client VAD is a prerequisite). 9 and 12 both touch `gating.py` — never in flight together. Step 22 touches everything — never in flight with anything.

**Progress note:** the user's priority order (9 → 10 → 11) is the same as the plan's within Group 2, and Group 0 is four small, independently verifiable commits that can ship in a single sitting before any of it.

---

## 4. Target tree

Grouping only — no behaviour change, no new package. Mapping old → new:

```
app/
  _tasks.py                          NEW   spawn() helper (R5)
  main.py                                  FastAPI wiring; workflow build moved to lifespan
  cli.py
  config/settings.py                       all tunables incl. AGENT_HTTP_PORT, TurnTiming, thinking
  agent/
    worker.py                              entrypoint + wiring ONLY (tools move out)
    diagnostics.py
    turn/                            NEW
      gating.py       ← app/agent/gating.py
      turns.py        ← app/agent/turns.py
      stop.py         ← NEW (extracted from gating.py:31-34,153-189)
      speech.py       ← NEW (local speech timeline, from input_filter)
    tools/                           NEW
      chat_tools.py   ← app/agent/chat_tools.py
      screen_tools.py ← app/agent/screen_tools.py
      parallel_tools.py ← app/agent/parallel_tools.py
      native_tools.py ← app/voice/native_tools.py
    personas/                        NEW
      personas.py     ← app/agent/personas.py
      persona_switch.py ← app/agent/persona_switch.py
      visual_replies.py ← app/agent/visual_replies.py
  voice/                                 unchanged layout: filter, health, recovery, runtime, network
  workflows/ screen/ meeting/ storage/ memory/ observability/ integrations/
docs/
  measurements/*.json                    moved from docs/, gitignored
  ARCHITECTURE.md ... (kept, dated)
  PLAN_2026-11-13.md                     was IMPROVEMENT_PLAN.md
PLAN.md                                  this file
```

Not proposed (no finding justifies it): replacing LiveKit, replacing LangGraph, per-participant audio, vector memory, Postgres, microservices. All are in `IMPROVEMENT_PLAN.md` F05/F08/F14 as later-stage product work.

---

## 5. DELETE vs REVIEW

Proven by repo-wide grep excluding `.venv/ __pycache__/ data/` (each had exactly 1 hit: its own definition).

### DELETE — safe
| Item | size | Proof |
|---|---|---|
| `app/meeting/events.py:149` `event_digest()` | 2 lines | 0 references; sole user of `import hashlib` (`:2`) |
| `app/storage/sqlite.py:14-16` `TranscriptStore(Protocol)` | 3 lines | 0 references; sole user of `from typing import Protocol` (`:5`); already out of sync with `Store` |
| `scripts/check_rtc_transport.py` | 3 298 B | 0 references |
| `scripts/check_voice_transport.py` | 3 561 B | 0 references |
| `scripts/check_voice_research.py` | 4 156 B | 0 references |
| `scripts/benchmark_voice.py` | 11 651 B | only referenced by the file above → transitively dead |
| `audit/` (4 md, 31 KB) | — | superseded by `PLAN_2026-11-13.md` + this file; unlinked from README |
| `LIVE_AGENT_COSTS.docx` | 40 944 B | binary duplicate of `LIVE_AGENT_COSTS.html` |

### REVIEW — do not delete without your call
| Item | Evidence | Why uncertain |
|---|---|---|
| `app/cli.py` | 0 imports, 4 doc references, runnable `python -m app.cli` | Real operator tool; README never mentions it |
| `scripts/check_audio_runtime.py` | 1 ref: `docs/WINDOWS_AUDIO_CRASH.md:24` | Documents the Windows soxr fix |
| `scripts/check_chat_latency.py` | 1 ref: `docs/CHAT_LATENCY_REPORT.md:22` | Becomes the benchmark for step 14 — keep |
| `scripts/export_transcripts.py` | 1 ref: `docs/SPEECH_FILES.md:6` | Operational utility |
| `tests/e2e/` | only `README.md`, collects nothing | Placeholder |
| `docs/*.md` (17 files) | Nov 6–7, stale (145 vs 370 tests) | Historical evidence; I only move the `.json` measurements |
| `IMPROVEMENT_PLAN.md` | Nov 13, 40 KB, superseded | Rename to `docs/`, don't delete |

**No unused dependency.** `uvicorn` (CLI: `Dockerfile:7`, `launch.py:154`), `pytest-asyncio` (plugin: `asyncio_mode=auto`), `ruff` (CLI) have no `import` but are live. All 13 runtime deps have direct imports.

---

## 6. Open questions (blocking decisions only)

1. **Git.** There is no repository. May I `git init` + baseline commit locally, and is there a remote/branch convention? Nothing in Phase 3 is reversible without this, so it gates everything.
2. **Product semantics for an unaddressed generation** (decides step 9's outcome table): today the agent generates a reply and the app *silently deletes it* (13.5 %). Should it be (a) cancelled and never spoken — status quo intent, or (b) allowed to answer a clear direct question even without the wake name, the wake name only widening the follow-up window? These are different state machines, and (b) would delete much of the gate.
3. **Local VAD — now the central decision, not a fallback.** Step 10 needs it only if the dependency-free classifier admits coughs. But step 9's *pre-generation* track needs it unconditionally, because disabling server activity detection leaves turn-taking to the client. May I add `livekit-plugins-silero` (local ONNX, no network, no charge, matches the pinned `livekit-agents==1.8.11`) if the spike shows the pre-generation path is worth having? Without it, step 9 ships the cancel path only and the plan's ceiling stays where it is.
4. **Provider spend.** Steps 9, 10 and 13 need live Gemini calls (≈20 turns each for the A/Bs, plus the spike). May I run them, and what is the cap?
5. **`data/` runtime artifacts.** 47 latency JSONL, 4 MB SQLite WAL, 482 KB `.wav` fixtures, 818 KB log. May I archive one baseline sample under `docs/measurements/` and purge the rest? They are gitignored and regenerable, but they are currently the only measured evidence.

---

## 7. Why this, in this order

The 13.5 % dropped-response rate is not a quality problem: it is an `await` placed **after** the model has already spoken, waiting for a transcript the provider only emits when generation *ends*. Prompt tuning and noise tuning cannot fix an ordering bug; they only change how often it fires. That is why step 6 measures first, step 9 removes the output-side ordering error, and only then do steps 10-11 tune what the model hears.

Group 0 comes before all of it for a different reason: a wrong `interrupted` flag is recoverable, a deleted transcript is not.

**Stop here. Awaiting "go" before any code change.**
