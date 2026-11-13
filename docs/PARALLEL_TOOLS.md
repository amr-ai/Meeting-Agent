# Parallel conversation and tools

Voice tools now use LiveKit's public `RunContext.update()` API. The first progress
update releases the model's tool dispatch while the coroutine continues under the
SDK async executor. Search, meeting context and chat operations use this lifecycle.
The existing Gemini nonblocking/WHEN_IDLE defaults are retained. No voice model,
VAD, PCM, resampler or browser audio settings were changed.

The prompt asks for one short, natural acknowledgment in the caller's language,
without a fixed sentence or repeated filler. New addressed questions can be handled
while work is pending. Final results use the SDK's idle/coalesced reply scheduling;
only scheduled updates from known authorized tasks can reopen the response gate.
Identical concurrent read/search calls are rejected by SDK duplicate detection.

Normal speech no longer cancels these background coroutines. Saying "stop talking"
silences speech, keeps work running, and suppresses that task's unsolicited spoken
completion even after another wake. Explicit task cancellation uses the SDK task
management tools for cancellable reads/searches. Writes are not advertised as
cancellable SDK tasks.

Research-to-chat jobs remain durable and independently processed (bounded by
`chat_workers`). The meeting-scoped `background_chat_tasks` tool checks or explicitly
cancels those jobs. Cancellation is checked before search, composition, reservation
and the atomic queued-to-sending transition. Already sent or in-flight messages are
not claimed to be recalled. Chat-only results stay in chat.

Service protocol is now 7: restart the CMD services to load these changes after
leaving any current call. Existing live processes are not automatically restarted.
The synthetic benchmark uses an ephemeral worker HTTP port to avoid a collision
with an already-running user worker.

## Verification

Final local checks: Ruff passed; full pytest suite **152 passed in 159.13s**.

`tests/unit/test_parallel_tools.py` exercises two outstanding operations, a new
spoken turn while both run, out-of-order completion, stop suppression, unknown
update rejection, explicit cancellation, durable queue concurrency and prevention
of restarting cancelled jobs. It also exercises the installed SDK executor with a
real RunContext, proving dispatch returns on progress while the work remains live.
That SDK contract test uses a deliberately blocked operation, not a timing guess.

The full existing regression suite also covers native audio gating, chat delivery,
research workflow and launcher compatibility. A synthetic credentialed voice probe
is separate from an actual Google Meet test. Its output excludes Meet playback and
must not be represented as a measured conversational overlap unless overlap is
explicitly measured. Earlier readiness failures (network restrictions and worker
HTTP port collision) occurred before any spoken turns and are not latency samples.

SDK lifecycle reference: https://docs.livekit.io/agents/logic/tools/async/

Credentialed synthetic regression (2026-11-07 12:01 UTC): 3/3 audible responses,
research job completed, exactly one locally captured outgoing message. End-of-input
to first received audio: 4775.6, 2971.7, 2912.9 ms. This is a small sequential voice
regression, not proof of simultaneous real Meet conversation. It ran while the
full local regression suite was also executing. Raw scoped results:
`VOICE_RESEARCH_PROBE.json`. The deliberately blocked SDK/unit tests establish
concurrency independently from provider/network response times.
