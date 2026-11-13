# Findings — ranked provisional architecture audit

Evidence labels and coverage limits are in 01_system_model.md. Impact frequency is unknown unless stated. Ranking uses qualitative impact × confidence / effort, not invented numerical scores.

## F1 — No proof that queued pixels are available to the answer turn
Area: screen correctness/latency. VERIFIED source: `app/voice/recovery.py:63` enqueues realtime image; `app/agent/visual_replies.py:25` later triggers a client-content turn through pinned plugin `realtime_api.py:859`. Installed `google/genai/live.py:186,279` explicitly documents nondeterministic realtime ordering and warns about mixing client-content/realtime input.

INFERRED root cause: FIFO transport order is being used as a proxy for model readiness. Late-frame answers can be stale or deny a share. High potential impact; medium causal confidence; occurrence rate unmeasured. Disproof attempted: images are already native realtime video, so changing payload from historical image to video alone does not solve it. Local queue ordering is correct but insufficient.

Options: (A) retain native Live, test request-scoped frame timing and provider-native function-response scheduling; (B) separate vision call returning grounded text, paying another inference and losing some latency. Recommend A experimentally, retain B for comparisons/research. Effort M, risk high because provider behavior needs acceptance. Verify correct random slide code, first grounded audio and subsequent ordinary-turn latency; do not count progress acknowledgment as success.

## F2 — Visual answer can expire silently
Area: reliability. VERIFIED: `app/agent/visual_replies.py:25` waits up to 30 s for several state predicates and may return without a terminal user-visible result; its finally block interrupts outstanding speech. No timeout-stage event is emitted on every exit.

INFERRED impact: an accepted request can appear forgotten during sustained speech or stale busy state. High confidence in control-flow possibility; frequency unknown. Disproof: epochs and stop checks are necessary and must remain; timeout is bounded, so this is not an infinite queue.

Options: (A) explicit per-request terminal states, reason telemetry and short deferred failure notice only if still relevant; (B) force speech immediately, risking interruption and stale answers. Recommend A. Effort S/M, low-to-medium risk. Verify busy-to-idle, deadline, disconnected input, stop and newer-question scenarios; exactly one terminal state, no speech after stop.

## F3 — Screen measurements cannot locate today's dominant delay
Area: observability/test validity. VERIFIED: pooled artifacts have eight visual retrieval timings but only one native image delivery event and no visual scheduling event. `screen_tools.py:123` assigns request_id; scheduler events omit it. Synthetic native-screen probes explicitly contain no screenshot.

Root cause: transport, tool, scheduler and audio events lack one complete request/frame/turn identity chain. Impact: successive optimizations cannot distinguish faster acknowledgment from grounded answer. High confidence, high operational impact. Disproof: decoder metrics exist and are useful; absence is end-to-end correlation, not absence of all telemetry.

Options: (A) add correlated stage events and deterministic pixel tests; (B) rely on console elapsed time/manual perception. Recommend A first. Effort S/M, low risk. Verify trace completeness and failure denominator. Preserve separate bridge and acoustic measurements.

## F4 — Decoder overload can become fresh-frame starvation
Area: capture reliability/scaling. VERIFIED: `receiver.py:161,181` has one bounded queue/serial executor; overflow or stale packet invalidates generation. `decoder.py:61` requires a keyframe after reset. Defaults permit high-rate H264 ingestion but JPEG emission only every two seconds.

INFERRED impact: overload can discard useful cached frames and require a future keyframe; head-of-line contention across shares can worsen it. Confidence medium; pooled logs show one packet drop, not widespread saturation. Disproof: decoding every dependency packet is necessary for H264; simply dropping all unsampled compressed packets is incorrect.

Options: (A) retain codec-safe decode, instrument queue age/keyframe recovery, isolate per-stream quotas if load tests prove contention; (B) PNG frame input simplifies dropping at documented 360p quality loss. Recommend A conditionally; do not rewrite codec path without reproducing overload. Effort M, medium risk. Verify two streams, scrolling, slow decoder and recovery latency with real encoded fixture packets.

## F5 — Completed tool records are retained without pruning
Area: maintainability/memory. VERIFIED: `app/agent/parallel_tools.py:15` inserts calls; completion changes status, on_event scans calls, and this class contains no removal path. INFERRED O(total lifetime tool calls) lookup/memory growth for a long session. High structural confidence, low present impact; no measured memory incident.

Options: (A) bounded completed tombstones, preserving active calls and late update correlation; (B) remove immediately, risking late SDK completion handling. Recommend A after higher priorities. Effort S, low risk with late-event tests. Verify thousands of completed calls remain bounded and late legitimate results still recognized.

## F6 — Context work grows with full transcript despite small output budget
Area: data/latency. VERIFIED: `memory/context.py:21` loads final utterances from the transcript, ranks in Python then limits selected results; output is character-truncated. INFERRED historical-session cost grows with transcript size, and tail slicing can remove useful earlier facts. No evidence that this caused the current audio delays.

Options: (A) bounded SQL retrieval plus indexed lexical search and section budgets; (B) embeddings with added dependencies/cost. Recommend A only after local profiling at realistic duration. Effort M, medium quality risk. Verify retrieval relevance in Arabic/English and build time at 1/10/100k rows.

## Looked suspicious but not established as broken
VERIFIED: compression/resumption are already enabled (`worker.py:65`); recommending enabling them as a fix would be redundant. Native current-screen capture does not call the standalone vision model (`workflow.py:26`); blaming every screenshot delay on an extra vision inference is incorrect. Crop/decoder work is off-loop. LangGraph is not on the audio frame path: no evidence that removing it improves voice latency. Strict presenter/generation checks prevent wrong-share answers; retain them. Unknown speaker labels reflect attribution uncertainty, not necessarily a broken microphone.

## Unresolved, not promoted to findings
GUESS: decoder CPU causes heartbeat timeout; current measurements do not prove it. GUESS: every 1008 is the same resumption collision; error bodies distinguish cases. INFERRED: multiple API processes would split in-memory screen/session state, but current launcher is single-process; capacity work needs an explicit deployment target. Original service exit remains unidentified; new supervision logging has not been exercised in an actual failure here.
