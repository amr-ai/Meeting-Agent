# Credentialed acceptance procedure

Use only an authorized meeting with informed recording/transcription consent.
Configure credentials and start the API and worker as described in README.md.
Do not claim a check passed until its output has actually been observed.

1. Connect the local page. Confirm one input publication and one agent output;
   verify native Gemini audio in the worker, without external STT/TTS.
2. Record 20 direct voice turns: short Egyptian Arabic requests, English requests,
   mixed terms (APIs, LangGraph, Machine Learning), silence and multi-turn context.
   Interrupt mid-answer and verify prompt stop, without old answer replay.
3. Request an explicit online search. Confirm one function invocation and
   LangGraph search request, inspect returned source URLs, and verify failures
   are acknowledged. Ordinary conversation must not invoke the graph.
4. Join a private Google Meet with the CLI, admit the bot, and inspect bridge
   publication/subscription. Confirm meeting speech is received and output is
   audible to another human participant. Check for echo/self-listening.
5. Record 10 meeting turns, alternating wake-name requests and unrelated human
   discussion. Check each Arabic/English wake variant, name mention false
   activations and late transcript suppression. Questions without a wake name
   are expected to remain silent in this V1.
6. Use two named participants and an unresolved participant. Inspect Recall
   transcript names, timestamps and participant IDs in stored output; do not
   infer live identity from mixed audio. Wait for the multilingual transcription
   delay before declaring events lost.
7. Briefly interrupt bridge network access. Observe reconnect, one microphone
   publication, correct remote track attachment and resumed conversation. Restart
   the worker; check re-dispatch and `context_recovered` or explicit failure.
8. Continue a real session for at least 60 minutes. Record provider reconnects,
   capability renewals, memory availability, audio failures, process memory and
   any silent gaps. Exercise interruptions and a search across reconnection.
9. Leave with CLI. Verify bot departure and LiveKit room cleanup. Wait for the
   final transcript artifact, inspect atomic reconciliation, and confirm repeated
   final polling adds no duplicates. Export before testing local deletion.
10. Aggregate raw latency JSONL. Report measured counts, P50/P95/max, errors and
    exact observation scope. Preserve test conditions and distinguish observed
    SDK timing from physical audio playback. Never label mock tests real service
    tests or claim one-hour stability from a short run.
