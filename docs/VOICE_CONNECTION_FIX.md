# Voice delay and missing replies: connection audit

Observed in the user's latest service log on 2026-11-07:

- Gemini disconnected with WebSocket 1006.
- Reconnect failed with 1008: `BidiGenerateContent session is already connected
  to an existing client`.
- The pinned Google plugin classified that failed reconnect handshake as fatal;
  AgentSession closed while the meeting itself remained connected.
- TLS certificate initialization blocked the worker event loop for 1496 ms and
  3355 ms. These startup stalls are separate from model generation latency.
- An addressed reply in the latest conversation was reported by the existing
  bridge as 320 ms, excluding Meet playback. The corresponding gate decision
  was immediate. This is not evidence that all responses are fast: the later
  connection failure left the session unable to answer.

## Changes

Certificate verification stays enabled. Reuse one verified SSLContext for HTTP,
Google async HTTP and WebSocket transports. Prepare that context and httpcore's
first-import state at worker warm-up, with an off-loop startup fallback. Concurrent
prewarm threads share a locked cache. The SDK's existing connection timeout stays
10 seconds. Public Google HttpOptions set WebSocket ping/timeout to 10/10 seconds
and close timeout to 3 seconds, reducing silent dead-transport waits.

`app/voice/recovery.py` is a narrow adapter for the pinned LiveKit Google 1.8.11
plugin. Only the exact 1008 resume-collision error receives one fresh-transport
fallback; the SDK replays its retained chat context with the same instructions,
tools and voice configuration. Normal SDK resumption remains enabled. Successful
connections renew the recovery budget, so an independent later disconnection does
not automatically become fatal. Consecutive failed handshakes remain bounded;
authentication, invalid model and other 1008 errors are not hidden or retried by
this adapter. A private extension is necessary because the plugin emits its fatal
error before returning its failed connection task. Contract tests cover this
version-pinned behavior; revisit the adapter when upgrading the plugin.

Recoverable connection errors now appear as reconnecting in the CMD conversation
log. Fatal failures explicitly report that voice is unavailable. A recoverable
error no longer sends the browser a fatal-error notification.

Voice model, voice selection, audio rates/PCM/resampling, VAD, native interruption
settings, response-gate authorization and background tool concurrency are retained.
Restart CMD services to load protocol 8; no active meeting is restarted by this work.

## Verification scope

Unit tests exercise actual installed Google client construction without any new
SSL context creation, exact-error matching, bounded fallback, preserved context,
renewed budget after success and propagation of unrelated fatal errors. These are
deterministic contract tests, not an induced real provider outage.

The first two credentialed synthetic trials encountered LiveKit signaling timeouts,
connected via region fallback, then received no user transcript at the Gemini
worker. Each stopped on the first audio timeout. The first run's zero successful turns are kept
in `VOICE_RECOVERY_NO_INPUT.json`; no latency improvement is claimed from that run.

Final checks: **161 tests passed in 61.22s**, Ruff passed. A direct actual Gemini
audio probe returned audio in 3679.2 ms with a transcript and no errors
(`VOICE_TRANSPORT_PROBE.json`). An isolated two-participant RTC probe delivered
audible frames (`RTC_TRANSPORT_PROBE.json`). After these separate checks, the
integrated synthetic RTC/Gemini regression returned **3/3 spoken responses**:
2832.8, 2322.4 and 3562.5 ms from end of synthetic input to first received audio.
These few samples show variable provider/network delay, not guaranteed production
latency or a proved return to every historical baseline. Recall chat is captured
locally in this probe: no actual Google Meet playback/delivery is measured. The
resume-collision fallback is tested deterministically, not through a forced real
provider outage.

Provider session-management reference:
https://ai.google.dev/gemini-api/docs/live-api/session-management
