import hashlib
import json
import math
import time
from datetime import datetime, timezone

ENDED = {'left', 'done', 'failed', 'fatal', 'call_ended'}


def parse_chat(sid: str, bot_id: str, data: dict, event_id: str,
               received_at: float | None = None, source: str = 'recall-live') -> dict:
    """Recall participant_events.chat_message, without invented provider IDs."""
    participant = data.get('participant') or {}
    content = data.get('data') or {}
    stamp = data.get('timestamp') or {}
    if not isinstance(content.get('text'), str) or not content['text'].strip():
        raise ValueError('Chat event lacks text')
    if stamp.get('absolute') is None or stamp.get('relative') is None:
        raise ValueError('Chat event lacks provider timestamps')
    timestamp = datetime.fromisoformat(stamp['absolute'].replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        raise ValueError('Provider timestamp must include timezone')
    # Webhook event ID identifies delivery, not a platform message ID. Artifact
    # download replays have different delivery IDs but the same semantic key.
    relative = float(stamp['relative'])
    if not math.isfinite(relative) or relative < 0:
        raise ValueError('Invalid provider relative timestamp')
    identity = [bot_id, participant.get('id'),
                {'absolute': timestamp.astimezone(timezone.utc).isoformat(timespec='microseconds'),
                 'relative': relative}, {'text': content['text'], 'to': content.get('to')}]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'id': key, 'meeting_id': sid, 'bot_id': bot_id, 'provider_event_id': event_id,
            'participant_id': str(participant['id']) if participant.get('id') is not None else None,
            'participant_name': participant.get('name') or 'Unknown', 'content': content['text'],
            'provider_timestamp': stamp['absolute'], 'event_time': timestamp.timestamp(),
            'relative_timestamp': relative,
            'received_at': received_at if received_at is not None else time.time(),
            'source': source, 'metadata': {'participant': participant, 'to': content.get('to'),
                                         'artifact_event_id': data.get('id')}}


def split_message(text: str, limit: int = 500, max_parts: int = 3) -> list[str]:
    if not isinstance(text, str) or not text.strip() or '\x00' in text:
        raise ValueError('A nonempty plain-text chat message is required')
    text = text.strip()
    # Count UTF-16 units conservatively so astral Unicode cannot exceed Meet's cap.
    def size(value):
        return len(value.encode('utf-16-le')) // 2

    if size(text) <= limit:
        return [text]
    parts, remaining = [], text
    budget = limit - 8  # Space for (1/3) numbering.
    while remaining:
        units, cut = 0, 0
        for index, char in enumerate(remaining):
            units += size(char)
            if units > budget:
                break
            cut = index + 1
        if cut < len(remaining):
            space = remaining.rfind(' ', 0, cut + 1)
            if space > 0:
                cut = space
            elif remaining.startswith(('https://', 'http://')):
                raise ValueError('A link exceeds the Google Meet message limit')
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
        if len(parts) > max_parts:
            raise ValueError('Message is too long; summarize it into at most three Meet messages')
    return [f'({i + 1}/{len(parts)}) {part}' for i, part in enumerate(parts)]
