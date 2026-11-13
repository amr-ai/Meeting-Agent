# Meeting chat

Khaled stores all Recall-delivered messages, including messages that do not name
him. Automatic replies are text in public Meet chat. The chat service has no RTC
room or Live audio session and never announces messages aloud.

## Start

Stop the old launcher and start `Join-Meeting.cmd` again. Existing bots do not gain
new event subscriptions retroactively. The launcher checks API protocol 6 to avoid
silently using an older API. No provider keys need re-entry; Google text chat uses
GOOGLE_API_KEY from the existing `.env`.

Use a consenting test meeting. Try an ordinary message, then `يا خالد، ممكن تلخص
اللي اتفقنا عليه؟`, then a short follow-up from the same participant. Ordinary
messages are stored without model calls. Direct mentions at the start, @mentions
and direct vocatives trigger replies. Third-person or quoted mentions are ignored.
A recent participant-specific thread permits clear question/continuation patterns
without a repeated name. Ambiguity defaults to silence; this is a conservative
heuristic, not perfect semantic addressee classification.

Voice examples:

- `يا خالد، آخر رسالة في الشات كانت إيه؟`
- `يا خالد، أحمد كان باعت لينك إيه؟`
- `يا خالد، ابعت في الشات إن الميتنج الجاي الساعة خمسة.`
- `يا خالد، إيه الآراء اللي اتقالت عن الـ Database؟`

Read/search/context/send are LangGraph tools bound to the current meeting. A
send requires an addressed voice turn and an explicit send instruction in the
actual current speech transcript. Quoted instructions in retrieved chat do not
authorize sending. Meet messages go to everyone; private sends are rejected.
Voice confirmation should be brief and must distinguish acceptance from uncertainty.

## Configuration

CHAT_AUTO_REPLY=true enables automatic directed text responses; false still stores
all incoming chat and permits explicit voice tools. CHAT_MODEL defaults to
`gemini-3.5-flash-lite`, a separate text model, not a replacement for Gemini Live.
Use `.env.example` for worker, follow-up, stale-message and rate-limit settings.
Defaults: 2 concurrent meetings, 60-second chat threads, 120-second maximum reply
age, 6 model attempts/minute/meeting, 3/minute/participant, 20 outgoing chunks/minute/
meeting and one-second spacing. A conservative workspace-wide 120/minute cap also
applies. Requests use only bounded relevant context, not the entire stored history.

## Storage and reliability

meeting_chat_messages stores complete original text, scoped participant identity,
provider and receive timestamps, direction/source, delivery/processing state,
reply relationships and metadata. A content/participant/provider-time fingerprint
handles duplicate webhook deliveries and artifact replay. Event delivery IDs are
not presented as platform message IDs. Identity comes from Recall, never voice
inference or names embedded in message text.

Incoming events commit before acknowledgment; a full wake queue loses only a
wakeup hint. Durable event/chat rows are polled again after database recovery or
restart. Cached answers and queued sends survive restarts. Generation can retry
within rate limits; a send interrupted by a timeout/process death becomes unknown
and is not automatically repeated. Only a definite 429 can retry, respecting
Retry-After. A permanent rejection is failed. HTTP 200 means accepted by Recall,
not proof of in-Meet visibility/read receipt. A matching bot echo can mark observed.

Meet chunks conservatively count UTF-16 units, max 500 units each and 3 chunks per
operation. Overlong requests must be shortened. A failed/uncertain chunk prevents
later parts from being sent out of sequence. Identical idempotency keys cannot
send different content. Self filtering uses exact outbox echoes and conservative
assistant-name collisions because the webhook has no documented is_bot flag.
A human using exactly the bot display name may therefore be suppressed; no
unverified bot identity is invented. Bot-side formatting changes can limit echo
recognition. Rate limits provide an additional loop bound.

All chat and stored speech are untrusted data. Text replies have no shell, file,
private-information or external-action tools. Explicit public web research uses
the existing SearchWorkflow; secret/private queries are excluded from this route.
Known configured credentials are redacted from generated replies and rejected
from outgoing sends. All admin APIs retain administrator authentication.

## Inspection

`python -m app.cli chat SESSION_ID` reads recent stored chat.
`python -m app.cli chat-metrics SESSION_ID` shows scoped timing samples.
`python -m app.cli latency SESSION_ID` retains existing browser voice metrics.

Authenticated API routes are GET `/api/meetings/{sid}/chat`, GET
`/api/meetings/{sid}/chat/metrics` and POST `/api/meetings/{sid}/chat/tools`.
The tools route validates read_meeting_chat, search_meeting_chat, get_chat_context
or send_meeting_chat, rejects model-supplied bot/meeting IDs and private recipients,
and supports bounded participant/text/time filters. The fixed sid belongs to the
server-trusted voice job. Use one API process, as with the existing SQLite design.

## Verification limits

Recall can expose only messages readable by this bot, normally after joining;
pre-join history is not assumed. Host chat controls or account restrictions can
block receiving or sending. Post-call artifacts can recover missing captured
messages, but cannot recreate data Recall never captured. No new frontend or
screen vision was added. See CHAT_TEST_REPORT.md and CHAT_LATENCY_REPORT.md for
actual checks and the remaining consenting Google Meet end-to-end test.


## Voice research reliability update

See [VOICE_TOOL_FIX.md](VOICE_TOOL_FIX.md). Spoken research-and-chat requests are
queued durably and run through LangGraph in the background. An explicit completed
voice transcript can dispatch the job even if the Live model only acknowledges.
A later model tool call is deduplicated against that same input ID. Queued is not
sent; the command window shows the final provider result. Clear compound requests
are supported. Generic latest-paper requests need a topic before research starts.
