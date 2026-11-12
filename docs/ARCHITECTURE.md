# Implemented architecture

## Participant-aware context and conversation journal

Participant membership and reported speech state are stored separately from
participant identity, with independent provider timestamps to resist stale
membership/speech updates. Missing names do not erase a known name. Stale speaker
timeline observations expire after 60 seconds. A reported speaker is metadata,
not inferred identity for every mixed-audio utterance.

Authenticated live-context feeds provide a cursor over stored human, assistant
and control observations. The worker polls outside the audio path, adds changed
rosters/timelines and new attributed Recall text through the supported chat-context
update API, and advances its cursor after application. Native user audio and its
assistant turns are already in Gemini's live session, so these are not replayed as
new voice questions. A bounded reminder of recent published replies reduces
accidental repetition. Provider compression remains enabled and earlier text is
retrievable from storage. No implementation can promise perfect verbatim recall
of an unlimited meeting in the live model context.

Native user final transcripts are persisted as Unknown until authoritative Recall
attribution is available. Assistant conversation events are recorded only after
at least one authorized audio frame has been handed to LiveKit, with SDK-provided
interruption flags. This measures publication, not actual words heard by every
meeting participant. Human provisional and authoritative Recall observations
retain their distinct provenance instead of fabricating a cross-clock identity.

A bounded worker queue decouples conversation HTTP writes from audio. A local
JSONL outbox records observations before transmission, replays them with stable
event keys on restart, and retains pending shutdown work. Queue pressure and
background failures are observable. Session retention/deletion also removes its
outbox files. The command window polls the same authenticated feed and displays
each cursor row once, including participant/speaker changes and interrupted
assistant replies.

Metadata synchronization coalesces speech-off/on flapping for the same last
reported person and defers model updates during active assistant speech. The
last reported name is explicitly not proof of a currently speaking participant.
An optional Web Audio energy probe measures input-end to received-output within
the bridge's own clock. Signed bridge capabilities scope writes to one session;
stored metrics and console messages never include the capability. These are
heuristic media-stream observations, not physical mouth-to-ear timings.

Stop commands revoke output/tool permissions, advance the gate epoch to invalidate
old output streams, cancel pending tools and invoke the SDK's force interruption.
Silenced state survives normal turn resets and can only be released by a fresh
wake name. A semantic stop function supplements short phrase detection. Automatic
false-interruption resumption is disabled. These changes require live meeting
acceptance testing before claiming reliable acoustic stop behavior.

The repository contains two processes from one Python codebase: FastAPI controls
meetings, tokens, persistence and recovery; a LiveKit AgentServer worker runs one
native Gemini Live AgentSession per dispatched room. Browser code is limited to
microphone acquisition, LiveKit WebRTC, track selection, playback and reconnects.
There is no Node backend, frontend framework or build pipeline.

## Audio

Local: browser microphone → LiveKit → Google plugin RealtimeModel → LiveKit → browser speaker.

Meeting: Google Meet → Recall virtual microphone in Output Media webpage →
one LiveKit microphone publication → Python AgentSession → Gemini Live native
audio → agent microphone publication → Output Media audio element → Recall → Meet.

The worker's RoomOptions binds input to `input-SESSION_ID`; the browser subscribes
only to `agent-SESSION_ID` with agent participant kind and microphone source.
Text input/output is disabled. No external STT or TTS generates ordinary voice
responses. Recall transcription is a separate archival/metadata path.

Gemini's native VAD and LiveKit interruption handling run normally. There is no
extra VAD model download, external turn detector, or endpointing LLM call.
The response output node checks a separate one-response permit before handing
frames to LiveKit. Name detection is normalized, case insensitive and bounded by
word boundaries. A new user speech event revokes prior permits and cancels
in-flight search. Missing or delayed transcripts fail closed. This preserves
full incoming native audio context but cannot avoid all unauthorized model-side
generation. See LIMITATIONS.md for the wake gate's race and quality boundaries.

## Session control

Authenticated local or join endpoints create UUID sessions with deterministic
room/input/agent identities. Room creation is idempotent and an explicit named
agent dispatch is reused if pending/running. Dispatch creation is serialized
inside the single API process. Token-embedded dispatch is deliberately avoided:
rejoining a surviving room would not retrigger it.

The signed bridge capability is carried in a URL fragment, exchanged by POST,
and renewed halfway through its short lifetime. Database status and hard expiry
are checked on every exchange. LiveKit JWTs permit microphone publish and
subscription within one room, without data or metadata mutation privileges.
Recall loads only the minimal bridge page. Provider credentials stay in Python.
The bridge reconciles tracks after participant restarts and uses bounded retries
after a terminal transport disconnect. LiveKit performs its own transient retries.

The API supervisor polls every 15 seconds, observes Recall status, re-ensures
dispatch, enforces session expiry, deletes ended LiveKit rooms and retrieves
finished transcript resources. API shutdown closes clients and drains its current
event processing work; meetings persist so a supervisor can resume after restart.
It intentionally does not end all paid meetings on every API deployment. Use the
leave CLI for immediate explicit cleanup. Expiry enforcement needs the API running.

## Events, transcript and memory

Recall webhook raw bytes are authenticated using its documented versioned HMAC
scheme, with a five-minute replay tolerance and constant-time comparison. Workspace
and legacy Svix header/secret sets are supported separately. Event IDs are unique
in the SQLite inbox. The handler persists before acknowledging. A bounded queue
only wakes the consumer; a full queue does not lose a durable event. Failed
processing retries up to five attempts and then remains visible as a dead letter.

SQLite uses WAL, parameterized SQL, a shared operation lock and foreign-key
cascades. Transcript utterances carry session/bot/participant IDs, verified speaker
name or Unknown, words, relative start/end, source, final flag, provider event ID
and metadata. Identity is never inferred from mixed audio. Participant snapshots
use provider event timestamps to reject stale updates. Final utterances resist
late partial updates. The completed resource replaces live segmentation in one
transaction, including its reconciliation marker, so differing chunk boundaries
do not create duplicate live/final utterances. Raw events remain an audit source.

The live model conversation, stored transcript and structured memory are distinct.
Gemini compression bounds its active context. `meeting_context` retrieves lexical
matches and recent final turns capped at 8,000 characters; it does not inject a
growing full transcript every turn. Worker recovery loads one bounded snapshot.
Only explicit `Decision:` / `Open question:` transcript labels become quoted
structured memory; other discussions remain transcript data. No speculative
automatic summary or separate RAG service is introduced.

The transcript interface is defined in storage/sqlite.py; a future PostgreSQL
adapter must also implement durable inbox/session transactions. The business
logic is independent of SQL connection details but replacing the whole repository
currently requires implementing the session/event methods as well.

## Tools and observation

Gemini `web_search` → Python function tool → compiled LangGraph StateGraph
(`validate` → `search` → END) → Tavily async HTTP → structured sources → Gemini.
No extra LLM participates in that graph. Timeout, HTTP failure and missing key
return explicit errors. Cancellation propagates. Normal speech never calls it.
An additional narrow context-retrieval tool reads existing meeting history.

Worker telemetry uses a bounded queue and async JSONL writer in a file per process.
Clock IDs separate monotonic domains. Metrics and errors contain no transcript or
audio payloads. Browser playback notifications are local console observations;
they are not combined with worker time. Production packet and acoustic tracing
would need additional synchronized instrumentation.
