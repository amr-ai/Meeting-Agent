# Screen/video research — checked 10 November 2026

## Sources and applicability
VERIFIED documentation means the source states the behavior, not that this deployment has demonstrated it. Google Developer API and Cloud documentation are not interchangeable endpoint contracts. Some ai.google.dev pages failed full retrieval (markdown response/redirect); search excerpts were available. Those excerpts do not establish undocumented limits. Installed source was inspected for the pinned packages: livekit-agents/livekit-plugins-google 1.8.11 and google-genai 3.0.0, as declared in pyproject.toml. No package upgrades were performed.

- [Google model page, Gemini 3.8 Live](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-live): VERIFIED model is intended for bidirectional realtime voice/video. `Settings.gemini_model` defaults to gemini-3.8-live; do not use old 2.5/3.1 model limits as exact 3.8 guarantees.
- [Google Live session management](https://ai.google.dev/gemini-api/docs/live-api/session-management): VERIFIED documented uncompressed limits are approximately 15 min audio and 2 min audio/video; compression extends context lifetime. This is not a promise of an uninterrupted WebSocket. Local worker already configures sliding-window compression and resumption.
- [Google asynchronous function calling](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/asynchronous-function-calling): VERIFIED 3.8 supports SILENT, WHEN_IDLE and INTERRUPT scheduling; omitted scheduling defaults to WHEN_IDLE on supported models. Streaming tool output should be combined into one function response. Scheduling controls response behavior, not an explicit frame-perception acknowledgment.
- [Google maintained Live reference app](https://github.com/GoogleCloudPlatform/generative-ai/blob/main/gemini/multimodal-live-api/gemini-live-studio/README.md): VERIFIED samples shared-screen/camera frames at 1 FPS. This is an engineering reference, not evidence that every application needs permanent video streaming.
- [LiveKit video documentation](https://docs.livekit.io/agents/multimodality/vision/video/): VERIFIED distinguishes video frame sampling/encoding and live video input. Installed plugin `realtime_api.py:846` encodes JPEG into LiveClientRealtimeInput.video. The application bypasses public push_video with an already encoded JPEG but uses the same transport message type.
- [Recall separate video documentation](https://docs.recall.ai/docs/how-to-get-separate-videos-per-participant-realtime): VERIFIED websocket PNG and H264 delivery. PNG screen capture is documented at 360p, 2 FPS; H264 is meeting-dependent, typically 240–1280px and 10–30 FPS. H264 preserves useful text detail but requires codec-aware processing. Recall recommends offloading decode and notes tunnel bandwidth can constrain throughput. No fixed capture-latency SLA is supplied here.
- [Google SDK issue 2290](https://github.com/googleapis/python-genai/issues/2290): VERIFIED a reporter describes audio/video resumption failures for 3.1 preview. It is evidence of a reported failure, not proof for current 3.8 or a verified remedy.
- [Google forum incident, 20 October 2026](https://discuss.ai.google.dev/t/gemini-3-8-live-1011-1008-session-drops-runaway-audio-and-output-transcription-with-unspoken-text/190881): VERIFIED report uses LiveKit 1.8.8 and 3.8 Live and describes 1011 followed by 1008 and transcript/audio discrepancies. Not the same pinned version and not proof of this application's root cause. No confirmed universal fix was established.
- [Google SDK releases](https://github.com/googleapis/python-genai/releases): release changes are reasons to rerun adapter contracts, not authorization to upgrade this working voice stack.

## Critical SDK evidence
VERIFIED: installed `.venv/Lib/site-packages/google/genai/live.py:186` documents ordered client content, potentially slower image preprocessing and cautions against interleaving client content with realtime input. At :279 it explicitly says realtime audio/video tokens enter context as available, trading deterministic ordering for responsiveness. Consequently INFERRED: frame enqueue before function response cannot certify visual grounding. A frame arriving after the original user turn can miss that generation's effective visual context.

VERIFIED: installed Google plugin `realtime_api.py:859` implements generate_reply through client content/turn_complete=True and cancels a pending generation when superseded. `app/agent/visual_replies.py:25` invokes that SDK method after sending realtime video. The project therefore mixes these message forms deliberately. This deserves a controlled pixel test; the SDK caveat alone does not prove all such turns fail.

## Intended media strategy
VERIFIED reference behavior: sampled image frames, not raw H264 into Gemini. 1 FPS is the conservative documented/reference starting point. INFERRED recommendation: preserve aspect ratio and benchmark readable full-frame JPEG versus crop; never stretch text into a square simply to match an example. Exact 3.8 Developer API frame-byte ceilings, media-resolution token schedule and effective frame sampling under this endpoint were not established in the fetched sources. The local 2 MB image ceiling and 1920 max edge are application choices, not claimed Google recommendations.

INFERRED: lower pixel detail can reduce preprocessing/usage but damage code/text reading; a crop can retain relevant text detail with fewer pixels. Compression can discard older context, so an old visual answer must not be treated as current pixels after reconnection. There is no reviewed documentation promising a per-frame 'seen' acknowledgment. Do not fabricate one with a sleep.

## Docs versus project gaps
| Topic | Documented/source behavior | Current implementation | Consequence |
|---|---|---|---|
| Media input | Sampled realtime frames | One requested JPEG via realtime video | Correct channel; timing remains uncertain |
| Turn ordering | Realtime processing not deterministic | Enqueue image then separate generated turn | Local ordering is not grounding proof |
| Function completion | Native scheduling supported | SILENT visual completion plus application scheduler | Two turn-control layers to validate/simplify |
| Frame cadence | Reference app 1 FPS | JPEG cache default 0.5 FPS, request refresh | Cached frame may lag motion |
| Session lifetime | Compression/resumption needed | Already configured | Not a missing-switch fix |
| Capture | High-rate H264 or low-detail PNG | H264 decode then JPEG | Codec continuity must survive sampling |
| Acceptance | Real pixels required for grounding | Existing fast probes use textual descriptions | Speech success is not vision success |

## Alternatives under the on-request constraint
All trade-off assessments are INFERRED, not measured gains.

A. Continuous low-FPS frames with change detection: improves availability before questions, adds ongoing visual usage/context and more recovery state. Violates the user's standing on-request-only preference unless explicitly opted in. Not default.
B. Event-driven keyframes: presenter/slide changes update local cache; upload only during an explicitly requested visual session. Good fit. Avoid uploading every detected change outside that session.
C. Local OCR/diff/cache: useful for stable slides/code, extra CPU and Arabic/layout recognition risk. Optional later; do not replace visual evidence with imperfect OCR silently.
D. Separate vision inference: explicit image-to-answer ownership and easier correlation; adds model call and spoken-result scheduling. Keep for history/comparison/research, or a measured fallback, not default automatic duplicate calls.
E. Speculative capture at speech start: local-only cache refresh can overlap speech without provider usage; speculative upload violates on-request policy before intent is known. Test local refresh first.
F. Cached last frame/change descriptions: cheap reuse within same revision, but stale and incomplete summaries can mislead after scrolling. Label cached evidence and invalidate on generation change; ask for current frame when required.

## Cost and unresolved research
INFERRED continuous 1 FPS for 10 minutes is 600 frames versus one frame for a single snapshot request; this is frame-count arithmetic, not a billing ratio. Session context and output usage also matter. Exact account pricing, free-tier effects and 3.8 media token accounting were not verified; no dollar estimate is justified. Record provider usage and use the current endpoint's price schedule before approving a probe.

Outstanding: full Developer API model-card/limits retrieval, exact-version AgentSession scheduling internals beyond the inspected plugin boundary, cookbook execution with real pixels, broader issue resolution history and controlled third-party benchmarks. None is represented as completed research. No historical bug report justifies changing automatic VAD, forcing activity events, or removing recovery without tests.
