# Latency report — 2026-11-06

## Follow-up: slow actual meeting reply

Three actual meeting replies had generated-audio → SDK handoff durations of
0.0568, 0.0548 and 0.0693 ms. Their authorization waits were 0.0400, 0.0376 and
0.0467 ms. These are worker-local component timings, not total response latency;
they exclude audio input, model processing before generation and Recall playback.
MEETING_GATE_TIMINGS.json records the scoped observations without meeting text.

The same session sent many metadata changes as one participant alternated between
speech and silence. Updates now retain the last reported speaker instead of
injecting repeated empty/nonempty speaker lists. Model context updates are also
deferred while assistant audio is playing. This reduces avoidable context traffic;
it is not proof that all perceived latency was caused by metadata updates.

A browser-side energy observer now measures detected input-speech end → first
received agent-audio energy using one `performance.now()` clock. It does not
modify or republish audio. Reports use the signed bridge session, omit its token
from storage, appear in the command window, and can be exported with:

```powershell
python -m app.cli latency SESSION_ID
```

The estimate uses energy thresholds, 20 ms observation intervals and a 300 ms
silence decision. Background noise, overlapping voices and matching an output
to the preceding utterance can affect it. It excludes capture before Recall's
virtual microphone and actual acoustic Google Meet playback. Suspended browser
AudioContexts can make observations unavailable; voice playback is independent
of this optional probe. Fresh-meeting observations are required before claiming
an improvement or reporting end-to-end P50/P95.

Gemini SDK user-state events were also found to occur around generation lifecycle
events rather than physical speech boundaries. New worker stages explicitly name
them as provider user-state observations. They are no longer emitted as measured
physical speech ends; older report-script rows remain historical observations.

## Credentialed diagnostic measurements

Actual service tests were run after a user reported no audio. Google SDK 3.0.0
was confirmed to default to `v1`; its Live WebSocket handshake returned HTTP 404.
Explicit `v1beta` succeeded with the same `gemini-3.8-live` model and key. The
worker now passes that version explicitly and the launcher checks a handshake.
One short text-triggered native audio diagnostic returned 12,960 non-silent PCM
bytes. This confirms generation, not acoustic browser playback.

Measured observations from this Windows host on 2026-11-06:

- Direct Gemini native audio input → first non-silent native output: **11,046 ms**
  in one synthetic English-speech trial. This excludes LiveKit and playback.
- Full LiveKit → application worker → Gemini → received LiveKit audio: **17,881.6 ms**
  in one successful synthetic Arabic-speech trial. The following English trial
  timed out after 25 seconds. That run completed 1 of 2 attempted turns; it did
  not reach the requested 20-turn sample.
- A later full-path run with a continuously published microphone timed out on
  its first trial after 25 seconds; it was stopped for investigation.
- Isolated LiveKit sender → LiveKit → receiver first-audible-frame transit:
  **362.1 ms** in one synthetic English-speech test. This excludes Gemini and
  physical speaker playback and is not a network RTT.

These scopes use the same local monotonic clock for each sender/receiver pair.
The full benchmark estimates source speech end from the AudioSource queue; it
does not measure a physical microphone or acoustic output. Its fixtures are real
Gemini-generated speech replayed at real-time speed, not mocked provider results.
No Recall meeting was joined. LiveKit reported the worker region as Israel.
Detailed network RTT/jitter/loss were not captured.

**The 1–2 second target was not achieved.** The sample is too small and unreliable
for meaningful population P50/P95 claims. The failed test runs are retained in
the diagnostic evidence; they must not be represented as successful 20-turn
performance verification. Earlier harness attempts had a health-port collision
and an output-track readiness mistake; those setup failures are not voice turns.

The isolated transport result suggests investigating Gemini native audio
processing, response selection and endpointing next, but it does not conclusively
attribute the full delay. Native endpointing is explicitly configured at 300 ms;
that setting alone did not establish reliable low latency. There was no silent
model or speech-pipeline substitution. Browser and meeting performance remain
unverified. See latency-measurements.json for the retained measured results.

## Implemented observations

Each worker has a unique clock ID, session ID and per-user-turn UUID. JSONL rows
carry a monotonic observation time and UTC wall time. Durations are calculated
only between observations with the same worker clock ID. Files are separate per
process, and writes use a bounded async queue outside the audio loop.

Stages include speech start, worker-observed speech end, final input transcript,
agent state changes, first generated audio seen by the output node, first
authorized frame handed to the LiveKit SDK, provider metrics, tool start/end,
recovery context availability and errors. Provider metric fields are used only
when actually supplied. Native VAD boundary accuracy and provider metrics need
live validation; their absence must not be replaced with invented timestamps.

The report script emits per-turn observations and nearest-rank P50/P95/max for
speech-end observation → SDK audio handoff. It includes observed error rate.
This aggregate is neither Gemini-only model latency nor mouth-to-ear latency.
Tool turns may legitimately take longer; `tool_started` marks them in the data.

The browser prints a local audio-element playback event with performance clock
origin. It does not claim to observe a first audible phoneme each turn and is
not merged with worker clocks. Exact first published/received packet, separate
endpointing/network/playback delays, synchronized total latency and network
conditions remain unmeasured. Controlled loopback/acoustic instrumentation is
needed before reporting those quantities.

## Engineering choices

Native audio eliminates a serial external STT → text model → TTS path. The
LangGraph workflow runs only for search requests. SQLite writes and Recall
polling run in the API process; neither is awaited by the voice audio node.
No deliberate extra audio resampling or batching is added. Only one input track
and the intended agent output track are used. Native Gemini VAD handles turns;
there is no separate endpointing-model roundtrip.

Meeting gating now accepts the earliest cumulative partial transcript containing
a wake name. The original 350 ms final-only gate was incorrect for the installed
Google plugin: actual meeting telemetry showed generated audio followed by an
authorized final roughly three seconds later, so no frame was handed to LiveKit.
The corrected gate waits up to five seconds for partial/final authorization and
records its wait and decision stages. Positive partials can release audio early;
final-only fallback can add seconds. Revised meeting latency remains unverified.
Recall's delayed multilingual transcript path is excluded from wake activation.

## Reproduce

Run at least 20 local voice turns and 10 consenting meeting turns, including
Arabic, English and mixed technical discussion. Save raw JSONL, browser logs,
provider region/model, timestamp, network type and test observations. Then run:

```powershell
$files = (Get-ChildItem data/latency-*.jsonl).FullName
python scripts/latency_report.py $files --output data/latency-report.json
```

Compare direct and Recall trials using the same model, region and utterances.
Do not subtract unrelated monotonic clocks or infer meeting-added latency from
unsynchronized server/browser timestamps. Investigate provider generation,
endpointing, authorized-output waiting and playback separately if delays appear.
