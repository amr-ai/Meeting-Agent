# Chat latency impact — 2026-11-07

## Native voice path

The 12 protected components in CHAT_VOICE_INVARIANTS.json match their pre-extension
hashes: model factory, room audio options, session options, output publication node,
original input/user-state callbacks, wake/stop gate, Windows audio runtime,
browser media/app/orb, and MeetingLink synchronization. Added work is meeting-only
tool registration plus a small transcript observer. Chat response generation runs
in the API process, independently of the native voice worker.

Historical identical addressed-fixture meeting-mode baseline before this feature:
2678.0, 11019.7, 6688.9 ms. After adding the tools: 3584.2, 2877.7, 2154.9 ms;
three of three non-silent responses. Both runs use the same model and cached
synthetic inputs through real LiveKit/Gemini. These are small sequential samples,
not a controlled simultaneous-load comparison or proof of a speedup. Both exclude
Recall/Meet/browser acoustic playback. A fresh in-Meet concurrent test is required
before asserting end-to-end voice non-regression. No audio configuration was tuned.

## Controlled offline chat load

`scripts/check_chat_latency.py` injected and durably stored 100 synthetic chat
messages while the actual output node forwarded 100 unchanged PCM frames at a
20-ms schedule. It compared another 100-frame run with chat disabled. No frames
were dropped or altered. Output-node handoff median/P95 was
0.0601/0.1048 ms without chat and
0.0526/0.0658 ms with chat. Event-loop scheduling jitter is
reported separately in CHAT_LOAD_MEASUREMENTS.json and varies with this Windows
host; these results do not establish causality or measure human response latency.

The local load used asynchronous mock generation (30 ms) and mock provider send
(20 ms). Its measured wall times include local scheduling/database work. Those
mock stage timings are never presented as actual Gemini or Recall latency.
All 100 incoming texts were persisted; background classification may still be
pending when the finite load window ends, and remains durable in SQLite.

## Chat stage measurements

The service records provider timestamp to server reception, chat-row persistence,
mention detection, model text generation, the whole reply workflow (which can
include requested web search), provider send, and reception to accepted send.
The latter includes queueing. `generation_started` is a rate-control counter,
not a latency observation. Provider-vs-host clock skew can affect reception delay.
Provider HTTP acceptance is not an acoustic or in-Meet visibility timestamp.

The one actual synthetic text-model smoke request took 6109.2 ms. It is one sample,
not P50/P95. Actual Recall send/visible-message latency was not measured because
no fresh consenting Meet was available. GET /api/meetings/{sid}/chat/metrics or
`python -m app.cli chat-metrics SESSION_ID` exposes measurements for the real test.

No complete chat history is continuously injected into the voice session. Context
retrieval is bounded and on demand; original messages remain intact in storage.
