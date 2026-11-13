# Meeting chat test report — 2026-11-07

Before changes: **73 passed in 121.74 seconds**.
Final full suite: **133 passed in 124.30 seconds**.
Ruff, Python compilation and pip dependency checks passed. No dependency changes.
The 60 additional checks cover unit behavior, durable storage, real client request
construction, LangGraph routing and mocked HTTP/webhook integration.

## Automated coverage

- Exact documented Recall live/artifact shapes, original Unicode text, delivery
  IDs versus artifact event IDs, unknown names and participant name changes.
- Duplicate webhook delivery, equivalent ISO timestamp forms, out-of-order events,
  durable inbox wake overflow, restart replay and temporary database read errors.
- Direct Arabic/English/code-switch mentions, @mentions, lookalike names, quoted
  and third-person references, participant-specific follow-ups and expiry.
- Plain messages remain stored with no text-model call; disabled automatic replies
  still preserve all messages. Self-name collisions and exact outgoing echoes
  are suppressed and do not loop.
- Two simultaneous participants keep their request identities; an ended call
  cannot receive a response generated just before it ended.
- Stable sending keys, concurrent duplicate attempts, cached generation recovery,
  unknown timeout/crash outcomes, definite 429 retry/Retry-After, 400 rejection,
  ambiguous 503 response, chunk limits, Unicode, long URLs and partial send failure.
- Authenticated APIs and server-bound meeting/recipient IDs, LangGraph read/search/
  send/context, participant/time filtering, cross-meeting isolation and tool errors.
- Explicit current voice-send intent, private-recipient rejection, quoted or
  reported instructions cannot authorize sending, gate/stop authorization, and
  configured-secret redaction/rejection. The text reply model has no action tools.
- Relevant written/spoken context is combined on demand. Existing retention and
  deletion cascade to chat, thread and metric rows. Older event schemas migrate
  idempotently.
- Actual application audio output publishes frames while chat generation is
  awaiting IO. Local tool count remains three; meeting adds four tools.
- Signed webhook -> durable event -> chat row -> selective response -> actual
  RecallClient HTTP construction -> outgoing row; requests use MockTransport,
  not a live Google Meet chat. Metrics are eventually persisted after acceptance.
- Post-call participant-event artifact recovery fills only missing messages,
  deduplicates captured live events, stores artifact IDs and never replies to
  historical requests from an ended meeting.

## Actual provider checks

One credentialed text-only Gemini request with synthetic Arabic input returned
151 text characters in 6109.2 ms, using CHAT_MODEL=gemini-3.5-flash-lite. No meeting
content or chat message was sent. CHAT_TEXT_SMOKE.json records this narrow scope.

Three cached synthetic Arabic, English and mixed questions passed through an
isolated LiveKit room and the actual native Gemini voice worker after adding chat
tool declarations. All returned non-silent native audio: 3584.2, 2877.7 and 2154.9 ms
from estimated input end to first received audio. No Google Meet was joined.
The native model, voice, resampling, VAD, gate and publication node stayed unchanged.
See CHAT_NATIVE_AUDIO_CHECK.json and CHAT_VOICE_INVARIANTS.json.

## Not yet verified

No fresh authorized Google Meet was supplied for this task. Receiving chat from
an actual Meet, visibility of sent messages, in-call follow-ups, real spoken read/
send commands and combined live voice/chat latency still require the consenting
end-to-end sequence in MEETING_CHAT.md. Mocked API success is not live delivery
proof. The code does not claim exactly-once provider delivery when a POST outcome
is ambiguous, pre-join history, perfect self identity or perfect addressee detection.
