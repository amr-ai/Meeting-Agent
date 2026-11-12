import asyncio
import json
import logging
import time

import httpx
from pydantic import SecretStr

from app.meeting.chat.models import ENDED, split_message
from app.storage.chat_repository import ChatRepository

log = logging.getLogger(__name__)


class ChatSender:
    def __init__(self, store, recall, settings):
        self.store, self.recall, self.settings = store, recall, settings
        self.repo = ChatRepository(store)
        self.locks: dict[str, asyncio.Lock] = {}

    async def send_chat_message(self, sid: str, message: str, key: str, reply_to=None):
        meeting = await self.store.get(sid)
        if not meeting or meeting['mode'] != 'meeting' or not meeting['bot_id']:
            raise ValueError('Chat is available only for this bound Recall meeting')
        if meeting['status'] in ENDED or time.time() > meeting['expires']:
            raise ValueError('Meeting has ended')
        for name in type(self.settings).model_fields:
            value = getattr(self.settings, name)
            if isinstance(value, SecretStr) and len(value.get_secret_value()) >= 8 and value.get_secret_value() in message:
                raise ValueError('Outgoing message contains configured credentials; no message was sent')
        parts = split_message(message)
        rows = await self.repo.outgoing(sid, meeting['bot_id'], self.settings.assistant_name,
                                        parts, key, reply_to)
        for row in rows:
            await self.deliver(row['id'])
        return await self.result(sid, key, len(parts))

    async def result(self, sid, key, count):
        rows = await self.store.rows('''SELECT id,delivery_status FROM meeting_chat_messages
          WHERE meeting_id=? AND idempotency_key IN (''' + ','.join('?' for _ in range(count)) + ')',
          (sid, *(f'{key}:{i}' for i in range(count))))
        return {'recipient': 'everyone', 'messages': rows,
                'accepted': bool(rows) and all(r['delivery_status'] in {'accepted', 'observed'} for r in rows),
                'note': 'accepted means the provider accepted the request, not proof that participants read it'}

    async def deliver(self, mid):
        rows = await self.store.rows('SELECT * FROM meeting_chat_messages WHERE id=?', (mid,))
        if not rows:
            return
        sid = rows[0]['meeting_id']
        async with self.locks.setdefault(sid, asyncio.Lock()):
            row = (await self.store.rows('SELECT * FROM meeting_chat_messages WHERE id=?', (mid,)))[0]
            if row['delivery_status'] != 'queued' or row['next_attempt'] > time.time():
                return
            part = json.loads(row['metadata']).get('part', 0)
            if part:
                previous_key = row['idempotency_key'].rsplit(':', 1)[0] + ':' + str(part - 1)
                previous = await self.store.rows('SELECT delivery_status FROM meeting_chat_messages WHERE meeting_id=? AND idempotency_key=?', (sid, previous_key))
                if not previous or previous[0]['delivery_status'] not in {'accepted', 'observed'}:
                    if previous and previous[0]['delivery_status'] in {'unknown', 'failed', 'cancelled'}:
                        await self.store.execute("UPDATE meeting_chat_messages SET delivery_status='cancelled',error='previous_part_not_confirmed' WHERE id=?", (mid,))
                    return
            meeting = await self.store.get(sid)
            if not meeting or meeting['status'] in ENDED or time.time() > meeting['expires']:
                await self.store.execute("UPDATE meeting_chat_messages SET delivery_status='cancelled',error='meeting_ended' WHERE id=?", (mid,))
                return
            recent = await self.store.rows('''SELECT COUNT(*) AS n,MAX(event_time) AS latest
              FROM meeting_chat_messages WHERE meeting_id=? AND direction='outgoing'
              AND delivery_status IN ('accepted','observed','sending','unknown') AND event_time>?''',
              (sid, time.time() - 60))
            global_rate = await self.store.rows("SELECT COUNT(*) AS n FROM meeting_chat_messages WHERE direction='outgoing' AND delivery_status IN ('accepted','observed','sending','unknown') AND event_time>?", (time.time() - 60,))
            if recent[0]['n'] >= self.settings.chat_send_per_minute or global_rate[0]['n'] >= 120:
                await self.store.execute('UPDATE meeting_chat_messages SET next_attempt=? WHERE id=?', (time.time() + 10, mid))
                return
            latest = recent[0]['latest']
            if latest:
                await asyncio.sleep(max(0, self.settings.chat_send_interval_seconds - (time.time() - latest)))
            # Recheck after a rate-limit wait: a call may end while we are waiting.
            meeting = await self.store.get(sid)
            if meeting['status'] in ENDED or time.time() > meeting['expires']:
                await self.store.execute("UPDATE meeting_chat_messages SET delivery_status='cancelled' WHERE id=?", (mid,))
                return
            async with self.store.lock:
                claimed = await self.store.db.execute("UPDATE meeting_chat_messages SET delivery_status='sending',attempts=attempts+1,event_time=? WHERE id=? AND delivery_status='queued'", (time.time(), mid))
                await self.store.db.commit()
                if claimed.rowcount != 1:
                    return
            start = time.perf_counter()
            status, error = 'accepted', None
            retry_delay = 5
            try:
                # This POST deliberately bypasses the generic Recall retry helper.
                await self.recall.send_chat_message(meeting['bot_id'], row['content'])
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code == 429 and row['attempts'] < 2:
                    status, error = 'queued', 'provider_rate_limit'
                    try:
                        retry_delay = min(300, max(1, float(exc.response.headers.get('Retry-After', '5'))))
                    except ValueError:
                        retry_delay = 5
                else:
                    status, error = ('failed' if 400 <= code < 500 else 'unknown'), f'http_{code}'
            except (httpx.TransportError, TimeoutError):
                status, error = 'unknown', 'ambiguous_transport_failure'
            except asyncio.CancelledError:
                # Persisted 'sending' will become unknown at restart; never replay it.
                raise
            except Exception as exc:
                status, error = 'unknown', type(exc).__name__
            await self.store.execute('''UPDATE meeting_chat_messages SET
              delivery_status=CASE WHEN delivery_status='observed' THEN 'observed' ELSE ? END,
              next_attempt=?,error=? WHERE id=?''', (status, time.time() + retry_delay, error, mid))
            await self.repo.metric(sid, mid, 'provider_send', (time.perf_counter() - start) * 1000)
            if status == 'accepted' and row['reply_to_message_id']:
                incoming = await self.store.rows('SELECT * FROM meeting_chat_messages WHERE id=? AND meeting_id=?',
                                                (row['reply_to_message_id'], sid))
                if incoming and incoming[0]['participant_id']:
                    await self.store.execute("""INSERT INTO chat_threads VALUES (?,?,?,?)
                      ON CONFLICT(meeting_id,participant_id) DO UPDATE SET
                        last_reply=excluded.last_reply,last_event=MAX(chat_threads.last_event,excluded.last_event)""",
                      (sid, incoming[0]['participant_id'], time.time(), incoming[0]['event_time']))
                if incoming:
                    await self.repo.metric(sid, mid, 'total_chat_response', (time.time() - incoming[0]['received_at']) * 1000)
            log.info('chat_send_result', extra={'session_id': sid, 'event_id': mid, 'error_type': error})

    async def drain(self):
        rows = await self.store.rows("SELECT id FROM meeting_chat_messages WHERE direction='outgoing' AND delivery_status='queued' AND next_attempt<=? ORDER BY received_at LIMIT 10", (time.time(),))
        for row in rows:
            await self.deliver(row['id'])
