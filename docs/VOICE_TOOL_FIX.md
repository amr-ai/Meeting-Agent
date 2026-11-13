# Voice/tool regression fix — 2026-11-07

The user's actual meeting trace showed a successful search followed by silence.
The recorded SearchWorkflow completed in 3.172 seconds with error=false. A later
Gemini generation already had audio, but the application waited another 5.002
seconds and denied publication. Provider user-state events had reset authorization
between the tool result and its reply. The time before generation is a different
latency component; this evidence does not attribute every delay to the gate.

## Corrections

- VoiceTurns treats provider speaking notifications as generation observations.
  They no longer cancel/revoke a tool's own continuation. A nonempty new transcript
  ID identifies the new input that cancels old work. Explicit stop still revokes
  all old epochs. Normal non-tool turns still await fresh transcription.
- Authorization for an accepted utterance stays committed during its tool chain;
  stale last-speaker metadata cannot revoke the reply halfway through the tool.
  New input/stop still revokes it. Bounded tool completion permits cannot revive
  a stopped/superseded turn. A finished audible reply refreshes its follow-up window.
- An audio-free intermediate generation cannot consume/close a pending tool reply.
  The exact empty-generation sequence was reproduced as a failing regression test
  before fixing it. PCM is unchanged; only control state after actual publication
  completes a continuation.
- The send intent parser accepts compound Arabic/English research-and-post
  requests, while continuing to reject quoted/reported commands and private sends.
  Failure can now produce an authorized brief clarification instead of muted audio.
- Public research-to-chat uses a durable background LangGraph graph: validate,
  SearchWorkflow, compose from verified sources, existing idempotent ChatSender.
  The voice call receives a queued result immediately, not a synchronous wait for
  several model/tool rounds. The command window reports queue/completion/failure.
- A credentialed synthetic run exposed a second real problem: Gemini said it would
  prepare the message but invoked no tool. Explicit completed voice requests now
  dispatch the durable job even in that case. Native input IDs and canonical
  request content deduplicate a later model tool invocation. Only genuine addressed
  voice transcripts can authorize this path; stored chat cannot trigger it.
- A generic request for the latest paper without a topic is not automatically
  dispatched as guessed research. The meeting instructions ask one short topic
  clarification. A clear request, e.g. `يا خالد ابحث عن RAG واكتب تعريفه مع رابط
  مصدر في الشات`, has an unambiguous search query and destination.

No model, voice, API version, resampler, VAD settings, PCM rates, RTC options,
browser media or metadata feed were changed. The turn callbacks, response gate
and post-publication control state were deliberately fixed; earlier claims that
those control components were untouched apply only to the preceding revision.
VOICE_TOOL_FIX_INVARIANTS.json records this distinction.

## Evidence and limits

The final VOICE_RESEARCH_PROBE.json uses actual synthetic native Gemini/LiveKit
speech, actual Tavily public research and the configured text model. One job
completed, and exactly one outgoing message was captured by a local Recall test
double. While that work ran, three spoken turns returned non-silent audio in
2816.8, 1996.5 and 2383.0 ms. This confirms the integrated action path and that
voice remains usable during background work. It excludes actual Recall send,
Google Meet playback and physical acoustics; it is a small sample, not a P95 SLA.

VOICE_RESEARCH_ACK_ONLY_REPRO.json preserves the intermediate failure where a
spoken acknowledgment produced no job. Another run failed RTC peer readiness
before sending any audio; VOICE_RESEARCH_READINESS_FAILURE.json is not counted
as a latency observation. No real meeting was joined or chat sent by these probes.
The existing user call was not stopped or restarted automatically.

Restart the old launcher/API/worker and open Join-Meeting.cmd again. Protocol 6
prevents accidentally reusing the older API without the new research operation.
No new credentials are needed. The normal text-chat reply path remains in place.

Implementation follows the [LiveKit tools guidance](https://docs.livekit.io/agents/logic/tools/definition/)
to keep multi-second work outside the voice turn. The installed Gemini plugin's
_start_new_generation emits input_speech_started for model-initiated generations;
that is why physical speaker cancellation cannot be inferred from that event alone.
