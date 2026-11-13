# Reference analysis and reuse decisions

The original ZIPs were left unchanged. They were extracted to a sibling
`references/` directory for examination. This independent project does not import
either repository. SOURCE_INVENTORY.json records every supplied file, its size and
SHA-256 digest. Repository files are reference material; their agent instructions,
plugin skills and example prompts were not treated as instructions for this task.

## AgentCall / Pattern AI Labs

The repository contains a hosted-service client, Python and JavaScript launchers,
audio/visual bridge helpers, examples, UI templates and service API guides.
The simple meeting assistant uses collaborative hosted voice features and
transcript/participant events. The smart assistant adds a bounded context manager
and asynchronous background tools, then sends context updates and contribution
commands to proprietary voice infrastructure. Its tools.py explicitly contains
pseudo implementations; those were excluded. Multilingual notetaking records
audio and performs later model transcription; it is not a native interactive
speech-to-speech design.

The Python service client and bridge scripts illustrate lifecycle event routing,
state files, reconnects, tool commands, VAD-style transcript coalescing and replay.
The crash-recovery guide distinguishes persistent bot state from the client's
connection and excludes transient audio from replay. Its suggestion that some
duplicate replay is harmless was rejected for authoritative transcript storage.
The interruption guide and browser audio helper show why interruption needs
explicit playback control and why every short partial is not necessarily an
interruption request.

Retained ideas: independent lifecycle/event processing, bounded context,
asynchronous tool work, explicit shutdown and persistent recovery state.
Reimplemented those ideas using asyncio, LiveKit, SQLite and LangGraph. No Python
component was copied verbatim. Excluded AgentCall API credentials, hosted service
endpoints, collaborative GetSun/FirstCall commands, proprietary voice behavior,
plugin registration/updating, tunneling infrastructure, generated UI templates,
external TTS examples and delayed full-audio summarization.

AgentCall includes an explicit MIT LICENSE, copyright 2026 AgentCall, Pattern AI
Labs. Reusing substantial code would require preserving its notice. This project
uses architectural concepts only and does not claim to reproduce proprietary
collaborative voice infrastructure or any hosted service implementation.

## Recall Output Media + LiveKit

The reference is a TypeScript sample with an Express control server, React/Vite
page, LiveKit Agents worker and explicit bot creation script. create_bot.ts sets
the Google Meet web_4_core bot variant and a webpage camera Output Media URL.
browser_bridge.ts captures the virtual meeting microphone with getUserMedia,
publishes one microphone source and plays only the named agent's remote audio.
Track-selection helpers enforce agent identity, kind, source and media type.
It handles participant/track changes and reconnect reconciliation.

The server signs a separate short-lived session JWT and mints a least-privilege
LiveKit participant JWT. Explicit ensure_agent_dispatch handles page reloads
where the room survives but the previous agent job has ended. Its tests cover
dispatch state and microphone-only grants. recording_done_webhook.ts correctly
distinguishes a recording ID from a bot ID for asynchronous transcription.
The original worker runs separate STT, LLM, TTS and turn-detector components.

Retained architecture: Output Media virtual microphone/audio element media flow,
deterministic session identities, scoped JWTs, explicit reusable dispatch,
bridge-only input binding and exact agent track selection. Independently rewrote
the control server, Recall HTTP client, session signing, dispatch and agent worker
in Python. Independently wrote minimal browser JavaScript for the required media
operations. Replaced the speech pipeline with Google RealtimeModel native audio.
Added durable transcript processing, speaker metadata, LangGraph search and
telemetry rather than porting the sample's UI or pipeline.

The supplied archive has no top-level LICENSE file, although package.json
declares `license: MIT`. To avoid relying on incomplete attribution/permission
material, no source code was copied or mechanically translated. Provider schemas
and SDK calls were independently implemented against official documentation.

## Current API checks and decisions (2026-11-06)

Installed and inspected LiveKit Agents/Google plugin 1.8.11 and LiveKit API 1.2.2.
Verified constructors, AgentServer registration, RoomOptions participant binding,
realtime_audio_output_node, session close APIs, token fields and the plugin's
resumption/compression options. The Python dependency graph passes pip check on
Python 3.13.1 Windows. Gemini model construction passes without contacting Google.

Selected configurable `gemini-3.8-live`, listed in Google's current model catalog.
No standard text model or speech pipeline fallback is allowed. Account access is
unverified. Native transcript-based gating is needed because native automatic
responses can precede application authorization. Recall's multilingual mode
prioritizes accuracy and can deliver transcripts minutes later; it cannot be the
fast wake trigger. This is an explicit product constraint, not an assumed timing
optimization. Database/event work never runs in the native audio output loop.

Primary references used for implementation:

- [Gemini Live via LiveKit](https://docs.livekit.io/agents/models/realtime/gemini/)
- [Python Google realtime API](https://docs.livekit.io/reference/python/livekit/plugins/google/realtime/realtime_api.html)
- [Gemini 3.8 Live model](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live)
- [Google Live session management](https://ai.google.dev/gemini-api/docs/live-api/session-management)
- [Recall Output Media](https://docs.recall.ai/docs/stream-media)
- [Recall Create Bot](https://docs.recall.ai/reference/bot_create)
- [Recall event payloads](https://docs.recall.ai/docs/real-time-event-payloads)
- [Recall request authentication](https://docs.recall.ai/docs/authenticating-requests-from-recallai)
- [Recall real-time webhook configuration](https://docs.recall.ai/docs/real-time-webhook-endpoints)
- [Recall multilingual transcription](https://docs.recall.ai/docs/multilingual-transcription)
- [Tavily search](https://docs.tavily.com/documentation/api-reference/endpoint/search)
