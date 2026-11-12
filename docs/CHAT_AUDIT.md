# Chat extension audit — 2026-11-07

Before implementation: 73 tests passed in 121.74 seconds. Audited the existing
Python checkout, not the separate publication checkout. No Git operations are
part of this task.

- RecallClient creates one web_4_core Google Meet bot with the existing output
  media bridge, transcript and participant subscriptions. Chat adds one event;
  the media, transcription configuration and subscriptions are retained.
- FastAPI authenticates the existing Recall webhook, resolves a registered bot
  to a trusted meeting ID, and persists events before acknowledgment. EventProcessor
  is a durable SQLite inbox consumer, not an in-memory-only message queue.
- SQLite stores meetings, participants, timelines, transcripts and conversation.
  New chat tables use the existing Store/connection/lock and cascade on meeting
  deletion. Existing retention applies. Schema initialization migrates older events.
- Gemini Live/LiveKit audio runs in a separate worker process. The native model,
  API version, VAD, resampling, room audio options, publication node, response gate,
  stop/interruption logic and browser media are preserved. Only meeting-mode tool
  registration and a lightweight transcript observer were added to the agent.
- The existing LangGraph SearchWorkflow is reused by a text-only reply graph.
  Four chat operations run through a bound LangGraph ChatToolsWorkflow.
- MeetingLink metadata polling and live audio are unchanged. Chat is not added to
  that feed. Relevant chat is retrieved only by tools or bounded startup recovery.
- The existing telemetry remains unchanged. New chat_metrics measure chat stages.
  Invariant evidence is in CHAT_VOICE_INVARIANTS.json.

## Verified provider contracts

Recall uses `participant_events.chat_message`, with participant metadata,
absolute/relative timestamp and nested `data.text`/`data.to`. The webhook has no
platform message ID or documented is_bot field. We store the authenticated delivery
ID; artifact event IDs, when available, are separate metadata. See [receiving
chat](https://docs.recall.ai/docs/receiving-chat-messages) and [event payloads](https://docs.recall.ai/docs/real-time-event-payloads).

POST `/api/v1/bot/{id}/send_chat_message/` accepts `to=everyone` on Meet. The
platform-specific Meet limit is 500 characters, overriding the generic API field's
4096-character ceiling. Plain URLs are supported as text; private Meet recipients
and formatted hyperlinks are not implemented. See [sending chat](https://docs.recall.ai/docs/sending-chat-messages)
and [the endpoint](https://docs.recall.ai/reference/bot_send_chat_message_create).

After the call, authenticated bot recordings expose a done participant-events
artifact via `media_shortcuts.participant_events.data.participant_events_download_url`.
Download it without an Authorization header; parse the documented array of
`action=chat_message` events and replay through the same deduplication logic.
This is post-call recovery, not a promise of real-time backfill. See [download
schemas](https://docs.recall.ai/docs/download-schemas).

Existing dependencies provide all needed functionality: httpx 0.28.1,
aiosqlite 0.22.1, LangGraph 1.2.20, google-genai 3.0.0 and LiveKit Agents 1.8.11.
No dependency upgrades or alternative audio frameworks were introduced.
