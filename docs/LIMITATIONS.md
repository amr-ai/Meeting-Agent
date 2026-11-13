# Latest shared-runtime verification — 2026-11-07

The website is confirmed working well by the user. Six isolated synthetic audio
trials in local/meeting modes all returned audio, with latency ranging from
2.66 to 11.02 seconds. This does not establish equal efficiency or actual Meet
playback quality. Current changes preserve the working website and share its
native voice/interrupt options with meeting mode. See SHARED_VOICE_CHECK.md.

---

# Implementation boundaries and unverified behavior

## External verification

Update after the reported no-audio problem: credentialed Gemini Live connection
and audio generation, and isolated LiveKit transport, have now been verified.
Full-path tests revealed high latency and response timeouts, so reliable real-time
dialogue is still unverified. The API-version 404 was corrected without changing
the model. See LATENCY_REPORT.md for measured results. The paragraph below records
the original delivery's verification boundary, before these diagnostics.

No actual provider credentials or consenting test meeting were available in the
environment. Gemini Live, LiveKit WebRTC, Recall joining/output playback, Tavily
search, Arabic/code-switching quality, browser autoplay, interruption behavior,
provider reconnects, and one-hour stability remain implemented but unverified.
The pinned browser module returned HTTP 200; this is not a WebRTC/browser test.
Python 3.13.1 Windows dependencies were installed, imported and checked. Docker,
Linux, Python 3.11/3.12 and a packaged wheel deployment were not exercised. Run
from the source checkout; static assets and prompts are checkout-relative.

## Wake selection

The native Gemini plugin is used without a custom fork. It may automatically
generate output before its input transcript reaches application code. The output
node therefore gates actual published audio, and tools separately require
authorization. It cannot guarantee zero unauthorized model generation or billing.
It authorizes from cumulative partial input transcription as soon as a wake name
appears. While holding the first generated frame, it waits up to five seconds for
either a positive partial or a final decision. Missing authorization still
suppresses output. Final-only providers can add seconds of waiting; late final
events cannot rearm an already consumed response permit.
The worker keeps all incoming audio context rather than discarding meeting speech.

Meeting activation requires a configured wake-name token. A configurable 30-second
follow-up window permits the active caller to continue without repeating the name;
a different reported speaker needs a new wake name. Attribution delays and unknown
speakers limit this heuristic. Explicit stop closes the window until a new wake.
Wake words in quoted mentions can still activate it. This needs live field testing.
Local voice chat is independent: no wake name, roster polling or metadata injection.
After a local stop, the next normal user turn can receive a response.

## Attribution and transcript timing

Recall is authoritative for participants and transcripts. The bridge microphone
is mixed audio; the live model has no guaranteed live speaker identity. Only
verified Recall transcript fields attach speaker names. Unknown names remain
Unknown, and simultaneous speakers are not resolved by a voice-matching model.
Attribution corrections and segmentation are taken from the final transcript.

Recall multilingual `prioritize_accuracy` events may arrive 3–10 minutes late.
Thus immediate speaker-aware responses and freshly persisted context may lag
spoken conversation. The Gemini native conversation can remember recent audio,
but cannot claim that memory provides verified participant attribution. This
limitation is explicitly separate from speech-to-speech response latency.

Final reconciliation requires the transcript artifact to become `done` and
contain documented words/participant schema. Polling retries while the API runs.
An incompatible resource shape raises an observable error rather than inventing
transcript data. Dead-letter events need operator investigation. A bridge
disconnect can lose transient audio; there is no audio replay buffer.

## Long meetings and recovery

Context-window compression and native session resumption are configured through
the supported Google plugin. Its in-memory handle survives provider connection
renewal inside the worker; it is not persisted across a process crash. A new
worker retrieves a bounded stored transcript snapshot, which may be delayed.
This is partial recovery, not an assertion of seamless conversational continuity.
No one-hour stability claim has been verified.

Run API and worker under an external process supervisor. The API checks dispatch
and Recall status every 15 seconds. Bridge reconnect attempts are bounded; an
expired capability or lost page state may require an authorized new session.
Renewed capabilities remain in page memory and are not stored in localStorage.
A full bridge page reload after the original capability expires cannot renew it.
The hard session deadline ends meetings only while the API supervisor is running.
API shutdown preserves paid bot sessions to support recovery; use the leave CLI
when ending a meeting intentionally and verify Recall status if cleanup fails.

## Observation and deployment

Worker monotonic stages are observations, not the exact physical speech boundary
or packet-publication time. Browser audio-element `playing` events are not
per-turn acoustic speech starts. No synchronized end-to-end mouth-to-ear or
Recall-added latency measurement is implemented. No network RTT/jitter/packet
loss collection is currently persisted. See LATENCY_REPORT.md for precise scope.

V1 uses one administrator API key, one API process, SQLite and bounded in-memory
queues. It has no per-user tenancy, distributed dispatch lock or automatic secret
rotation. Transcript databases are not encrypted by this application. Restrict
file permissions and use encrypted storage as appropriate. Local deletion does
not delete Recall recordings, provider logs, backups or browser console logs.
The CDN module must be reachable by the user's browser and Recall's page.
There is no production readiness or performance guarantee.
