# Voice/tool regression correction — 2026-11-07

Final full suite: **145 passed in 203.79 seconds**. Ruff, compilation and dependency
checks passed. The recorded tool-continuation/empty-audio failure was reproduced
and fixed, plus compound voice research, background persistence and model-only
acknowledgment dispatch. Actual synthetic native voice/research returned three
voice replies (1996.5–2816.8 ms) and exactly one locally captured outgoing chat.
Actual Meet playback/delivery was not exercised; see VOICE_TOOL_FIX.md for scope.

---

# Meeting chat extension — 2026-11-07

Current full suite: **133 passed in 124.30 seconds** (73 before the extension).
See CHAT_TEST_REPORT.md for unit/integration scope, actual text/native-voice
checks and the unverified consenting Google Meet end-to-end test. Audio invariants
are unchanged; CHAT_LATENCY_REPORT.md distinguishes controlled mock chat load
from real LiveKit/Gemini measurements. This task made no Git updates.

---

# Shared voice follow-up — 2026-11-07

Current verification: 70 tests passed in 28.32 seconds; six of six isolated
synthetic RTC audio trials returned audio. Website source/options confirmed
against the user-approved working version. Both modes share SDK voice defaults.
See SHARED_VOICE_CHECK.md for exact changes, measured latency and live-meeting
limits. The preceding isolation-only approach was not treated as the final fix.

---

# Local voice regression correction — 2026-11-07

67 tests passed in 24.96 seconds. Ruff, Python compilation, JavaScript module
syntax checks and pip dependency checks passed.

Regression findings: meeting activation/stop instructions and incremental context
updates were shared with local chat; explicit stop required a fresh wake name even
locally. Local sessions now use a separate policy, SDK interruption defaults, no
metadata polling or recovery-instruction injection, and ordinary next-turn
activation after stop. Meeting-only browser latency observation remains enabled.
Assistant replies are persisted but no longer reinserted as metadata user messages.
An empty stored context now returns an empty string rather than a header.

New tests cover local conversation persistence without context synchronization,
separate local/meeting policies, SDK interruption options, and local stop recovery.
The corrected paths have not yet been verified in a fresh browser microphone
conversation or live meeting. Existing sessions were not restarted.

---

# Test report — 2026-11-06

## Meeting latency investigation and instrumentation

Latest full suite: **59 passed in 24.98 seconds**. JavaScript syntax checks passed
for latency.js, media.js, app.js and bridge.js; Node was used only as a syntax
checker, not as an application service. Worker-local evidence from three actual
meeting replies showed gate waits of 0.0400, 0.0376 and 0.0467 ms, so that gate
did not account for the user's perceived delay in those replies.

New mocked checks cover coalescing repeated speech-off/on for the same person,
deferring metadata updates during assistant speech, signed session scoping and
validation of browser-clock latency observations, and metric deduplication.
The new Web Audio measurements and latency improvements have not been verified
in a fresh live meeting after restarting the bridge/worker.

## Participant context, history, stop and console changes

Latest full suite: **56 passed in 21.75 seconds**. Ruff, compilation and pip
dependency checks passed. New coverage verifies participant joins/leaves and
speaker transitions with out-of-order updates; preservation of known names when
speech events have no name; deduplicated user/assistant history and interruption
markers; silent incremental model metadata updates; conversation outbox replay
after restart; session-scoped journal deletion; authenticated live feeds; console
cursor deduplication; Arabic/English stop phrases; cancellation of old output
epochs; and a fresh wake name being required after explicit stop in both modes.
These are local SDK/database and mocked HTTP tests. The new participant-aware
model behavior, semantic stop interpretation and audible meeting interruption
have not yet been verified in a credentialed live meeting.

## Meeting output gating correction

Actual meeting telemetry showed generated audio with no SDK audio handoff,
including turns whose delayed final input transcript contained a recognized wake
name. The installed Google plugin emits that final only when its generation
finishes. A final-only 350 ms authorization deadline therefore muted valid replies.
The worker now handles partial transcripts and waits up to five seconds for a
decision. Finals cannot grant a second audio permit after one was consumed.
New regression cases cover partial authorization, final arrival after the old
deadline, and suppression of unaddressed partials. **37 tests passed in 16.61
seconds**, and Ruff passed. A corrected real meeting reply still requires
restarting the existing worker and testing in the meeting; it has not been claimed.

## One-URL meeting launcher

The latest suite passed **34 tests** (15.73 seconds in the recorded run). New
mocked checks verify that the meeting helper asks for only one URL, sends one
authorized join request, requests leave on completion, and never retries an
ambiguous failed join into duplicate bots. Credentials are read from `.env` and
the ngrok token is passed through the child environment, never command arguments.
No real meeting was joined by these launcher tests.

## Windows native assertion mitigation

After the user reported the exact `LSX_FFT_BR == NULL` dialog, the worker gained
serialized native-resampler startup initialization and explicit 16 kHz mono
RoomIO input. Latest verification: **32 tests passed in 19.67 seconds**, Ruff
passed, and Python compilation passed. `pip check` reports no broken requirements.
The offline native stress check passed in **10 fresh processes**, each performing
**64 resampling operations across eight threads** after startup warmup: 640 total
stress operations without the assertion. No unsafe uninitialized reproduction
was run. Browser voice and one-hour stability have not been reverified for this
mitigation. See WINDOWS_AUDIO_CRASH.md for scope and the upstream issue.

## Subsequent no-audio investigation

The latest offline run after the endpoint fix passed **30 tests**. It includes a
regression guard for the actual Google plugin's explicit Live API version.
Test fixtures now isolate environment settings so real `.env` values cannot
change mock provider regions or credentials.

Credentialed checks subsequently verified `gemini-3.8-live` over `v1beta`, native
non-silent generated audio, one direct native audio-input reply, and an isolated
LiveKit audio loopback. Full-path trials produced one slow successful response
and multiple response timeouts; they did not verify reliable dialogue or complete
20 successful turns. See LATENCY_REPORT.md for exact numbers and scopes.
The historical pre-credential report below records the earlier implementation run.

Environment: Windows x64, Python 3.13.1. Runtime dependency resolution and SDK
inspection used an isolated virtual environment in the parent workspace.

## Executed successfully

- `python -m pytest -q`: **29 passed** (15.70 seconds in the final recorded full run).
- Ruff lint with import checks: passed after automatic import cleanup.
- `python -m compileall -q app scripts`: passed.
- `python -m pip check`: **No broken requirements found**.
- FastAPI and voice worker imports: passed against actual installed packages.
- Google RealtimeModel construction with native audio, session resumption and
  context compression: passed locally; no provider connection was attempted.
- `python -m app.cli --help`: passed.
- `python -m app.agent.worker --help`: passed.
- Editable package build/install: passed with setuptools 84.2.1 and wheel 0.48.0.
- HTTPS HEAD for pinned LiveKit browser ESM module: HTTP 200.

## Offline test scope

The suite checks canonical Google Meet URL validation against alternate schemes,
credentials, query redirects and hostile hosts; signed bridge expiry/tampering;
webhook raw-body signatures and replay tolerance; real LiveKit JWT grant scope
and TTL; Arabic/English wake normalization; one-response permits and revocation;
actual Agent output-node suppression using synthetic RTC frames; duplicate and
late partial transcript behavior; Unknown speakers; atomic final segmentation
replacement and rollback; durable inbox recovery and deduplication; authenticated
API lifecycle, bridge renewal/revocation and transcript access; signed webhook
ingestion; local data deletion guards; explicit dispatch reuse and replacement of
completed jobs; actual compiled LangGraph routing with mocked search HTTP;
validation errors, provider failure and cancellation; Recall payload and final
resource download with mocked HTTP; no credential forwarding to download URLs;
no duplicate bot creation retry on an ambiguous timeout; and clock-domain isolation
in latency aggregation.

The SDK classes, JWT signing, SQLite database and LangGraph are real local
implementations in these tests. RTC frames are synthetic. All external service
responses are mocks. API lifecycle tests use fake Recall and LiveKit clients.
The 15.70-second suite duration is **not a voice latency measurement**. A regression
test also confirms verified participant events resolve Unknown names without
letting out-of-order older participant updates overwrite the latest identity.

The first run exposed a test assumption that a LiveKit JWT contains `iat`; the
installed SDK uses `nbf`. The corrected assertion checks `exp - nbf`. The worker
CLI initially required full settings even for `--help`; environment loading was
moved ahead of worker startup and the help command now executes without secrets.

## Not executed

No actual Gemini connection or generated speech, LiveKit Cloud room, microphone,
browser WebRTC session, Recall bot, Google Meet playback, live speaker attribution,
Tavily result, real network outage, credential renewal across one hour, real voice
latency sample, consented 20/10-turn trials, or one-hour stability test was run.
No provider keys matching the relevant names were present in the environment.
No credentials were requested from or guessed for connected third-party accounts.

Docker build and alternate OS/Python compatibility were not tested. Browser code
has not been exercised in Recall's hosted browser. Use ACCEPTANCE.md to complete
these checks and append the actual evidence before marking an integration verified.
