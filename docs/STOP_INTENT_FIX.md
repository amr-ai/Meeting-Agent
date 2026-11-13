# Pause, task work and explicit stop are different events

The latest recorded user session ended with Gemini transport errors after a
research-to-chat request; it does not contain an explicit stop event. That is the
connection failure documented in `VOICE_CONNECTION_FIX.md`, not proof that the
stop detector fired in that session.

The code audit nevertheless identified two independent ways to lose a directed
conversation: the model could invoke stop_speaking without any spoken stop intent,
and follow-ups were rejected after 15 seconds of unchanged Recall identity even
though the conversation lease was 30 seconds. The substring stop matcher could
also confuse a phrase such as "خالد خلاص ابحثلي ..." with a command to stop.

Changes:

- Stop intent is a complete, direct command with optional wake name/politeness.
  Speaking state alone cannot turn an embedded stop word into an instruction.
  Quoted, hypothetical and negated stop words and plain thanks are not stop intent.
- Ambiguous cumulative partials such as "خالد خلاص" wait for the final transcript;
  a subsequent task instruction does not trigger stop. Unambiguous imperatives
  such as "اسكت" still support prompt interruption on partial transcripts.
- The stop_speaking tool validates a recent transcript in the current input epoch
  before muting. Invalid model calls are rejected while preserving the authorized
  current request. Repeated valid stop calls are idempotent.
- Within the existing bounded follow-up window, unchanged verified caller identity
  does not expire separately at 15 seconds. Another/multiple reported speaker,
  explicit stop and the existing overall follow-up timeout still require a wake.
- Silence and task completion are explicitly excluded from model stop intent.
  Background tasks keep their existing independence from voice stop and pauses.

No VAD, audio format, resampling, voice/model selection or media pipeline was
changed. This is a control-logic correction, not a claim to fix every provider
disconnection or all response latency. Protocol 9 requires restarting old services
after the current call. No active call was stopped by this change.

`tests/unit/test_stop_intent.py` covers 34 cases including false positives,
cumulative partials, stale metadata, stale model calls and real stop commands.
Existing gating, background work and tool-continuation regression checks also pass.
Final validation: **195 passed in 67.18 seconds**, Ruff passed.
These deterministic checks do not substitute for a long actual Meet session.
