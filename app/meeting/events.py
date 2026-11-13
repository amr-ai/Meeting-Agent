import asyncio
import hashlib
import json
import logging
import time

from app.meeting.chat.models import parse_chat
from app.storage.chat_repository import ChatRepository
from app.storage.models import Utterance
from app.storage.sqlite import Store

log = logging.getLogger(__name__)


def parse_utterance(sid: str, bot_id: str, data: dict, eid: str,
                    final: bool = True, source: str = 'recall-live') -> Utterance | None:
    words = data.get('words', [])
    if not words:
        return None
    participant = data.get('participant') or {}
    start = words[0].get('start_timestamp') or {}
    end = words[-1].get('end_timestamp')
    if 'relative' not in start:
        raise ValueError('Transcript lacks relative start time')
    return Utterance(meeting_id=sid, bot_id=bot_id,
        participant_id=str(participant['id']) if participant.get('id') is not None else 'unresolved',
        speaker=participant.get('name') or 'Unknown',
        text=' '.join(w['text'] for w in words), start=start['relative'],
        end=end.get('relative') if end else None, source=source, final=final,
        provider_event_id=eid, metadata={'language_code': data.get('language_code')})


class EventProcessor:
    def __init__(self, store: Store, queue_size: int = 256, assistant_name: str = 'Khaled'):
        self.store = store
        self.chat_names = {assistant_name.casefold()}
        self.wake: asyncio.Queue[None] = asyncio.Queue(maxsize=queue_size)
        self.stopping = False
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name='durable-event-processor')

    async def ingest(self, eid: str, sid: str, payload: dict) -> None:
        # Persist before acknowledging; an in-memory queue is only a wakeup hint.
        await self.store.ingest(eid, sid, payload)
        try:
            self.wake.put_nowait(None)
        except asyncio.QueueFull:
            pass  # Durable inbox is drained on the next wakeup.

    async def process(self, row: dict) -> None:
        payload = json.loads(row['payload'])
        event = payload.get('event', '')
        wrapper = payload.get('data', {})
        data = wrapper.get('data', {})
        sid = row['meeting_id']
        bot_id = wrapper.get('bot', {}).get('id', '')
        participant = data.get('participant') or {}
        if event.startswith('participant_events.') and participant.get('id') is not None:
            stamp = data.get('timestamp', {}).get('relative', 0)
            await self.store.execute('''INSERT INTO participants VALUES (?,?,?,?)
              ON CONFLICT(meeting_id,id) DO UPDATE SET
                name=CASE WHEN excluded.name='Unknown' THEN participants.name ELSE excluded.name END,
                updated=excluded.updated
              WHERE excluded.updated >= participants.updated''',
              (sid, str(participant['id']), participant.get('name') or 'Unknown', stamp))
            pid = str(participant['id'])
            await self.store.execute('''INSERT OR IGNORE INTO participant_state
              VALUES (?,?,1,0,-1,-1,?)''', (sid, pid, time.time()))
            if event in {'participant_events.join', 'participant_events.leave'}:
                present = int(event.endswith('.join'))
                await self.store.execute('''UPDATE participant_state SET present=?,
                  speaking=CASE WHEN ?=0 THEN 0 ELSE speaking END,membership_stamp=?,observed=?
                  WHERE meeting_id=? AND id=? AND membership_stamp<=?''',
                  (present, present, stamp, time.time(), sid, pid, stamp))
            if event in {'participant_events.speech_on', 'participant_events.speech_off'}:
                speaking = int(event.endswith('.speech_on'))
                await self.store.execute('''UPDATE participant_state SET speaking=?,speech_stamp=?,observed=?
                  WHERE meeting_id=? AND id=? AND speech_stamp<=?''',
                  (speaking, stamp, time.time(), sid, pid, stamp))
            current = await self.store.rows('SELECT name FROM participants WHERE meeting_id=? AND id=?',
                                           (sid, str(participant['id'])))
            if current and current[0]['name'] != 'Unknown':
                await self.store.execute('''UPDATE utterances SET payload=json_set(payload,'$.speaker',?)
                  WHERE meeting_id=? AND participant_id=? AND json_extract(payload,'$.speaker')='Unknown' ''',
                  (current[0]['name'], sid, str(participant['id'])))
                await self.store.execute('''UPDATE conversation SET speaker=?
                  WHERE meeting_id=? AND participant_id=? AND speaker='Unknown' ''',
                  (current[0]['name'], sid, str(participant['id'])))
        if event == 'participant_events.chat_message':
            await ChatRepository(self.store).receive(parse_chat(sid, bot_id, data, row['id'],
                row.get('received_at')), self.chat_names)
        if event in {'transcript.data', 'transcript.partial_data'}:
            item = parse_utterance(sid, bot_id, data, row['id'], event == 'transcript.data')
            if item:
                if item.speaker == 'Unknown' and item.participant_id != 'unresolved':
                    known = await self.store.rows('SELECT name FROM participants WHERE meeting_id=? AND id=?',
                                                  (sid, item.participant_id))
                    if known:
                        item.speaker = known[0]['name']
                reconciled = await self.store.rows(
                    "SELECT text FROM memory WHERE meeting_id=? AND kind='reconciled'", (sid,))
                if not reconciled:
                    await self.store.upsert_utterance(item)
                if item.final:
                    await self.store.add_conversation(sid,
                        f'recall:{item.participant_id}:{item.start}', 'user', item.text,
                        item.speaker, item.participant_id, source='recall-live')
                    # Only explicit labels become structured memory; retain the original quote.
                    for label, kind in [('decision:', 'decision'), ('open question:', 'open_question')]:
                        if item.text.casefold().startswith(label):
                            await self.store.execute('INSERT OR IGNORE INTO memory VALUES (?,?,?)',
                                (sid, kind, f'{item.speaker}: {item.text}'[:1000]))
        await self.store.execute('UPDATE events SET processed=1,error=NULL WHERE id=?', (row['id'],))
        if event.startswith('participant_events.'):
            self.store.speech_dirty.add(sid)

    async def run(self) -> None:
        while not self.stopping:
            try:
                rows = await self.store.rows('SELECT * FROM events WHERE processed=0 AND attempts<5 LIMIT 100')
            except Exception as exc:
                log.warning('event_inbox_retry', extra={'error_type': type(exc).__name__})
                await asyncio.sleep(1)
                continue
            for row in rows:
                try:
                    await self.process(row)
                except Exception as exc:
                    try:
                        await self.store.execute('UPDATE events SET attempts=attempts+1,error=? WHERE id=?',
                                                 (type(exc).__name__, row['id']))
                    except Exception:
                        log.warning('event_attempt_record_retry', extra={'event_id': row['id']})
                    log.error('event_processing_failed', extra={'event_id': row['id'],
                                                              'error_type': type(exc).__name__})
            try:
                await asyncio.wait_for(self.wake.get(), timeout=1)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self.stopping = True
        if self.task:
            await self.task


def event_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
