"""Incremental metadata/history synchronization outside the real-time audio path."""
import asyncio
import hashlib
import json
import logging
import time
from uuid import uuid4

import httpx

from app.config.settings import Settings

log = logging.getLogger(__name__)


class MeetingLink:
    def __init__(self, agent, http: httpx.AsyncClient, settings: Settings, sid: str, telemetry):
        self.agent, self.http, self.settings = agent, http, settings
        self.sid, self.telemetry = sid, telemetry
        self.url = settings.api_base_url.rstrip('/') + f'/api/meetings/{sid}'
        self.headers = {'Authorization': 'Bearer ' + settings.admin_api_key.get_secret_value()}
        self.pending: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self.cursor = 0
        self.fingerprint = ''
        self.visual_context = {}
        self.last_reported_speakers: list[dict] = []
        self.tasks: list[asyncio.Task] = []
        self.outbox = settings.database_path.parent / f'conversation-{sid}-{uuid4().hex}.jsonl'
        self.journal_lock = asyncio.Lock()
        self.journal_tasks: set[asyncio.Task] = set()

    def start(self, *, sync_context: bool = True) -> None:
        self.tasks = [asyncio.create_task(self.send(), name='conversation-writer')]
        if sync_context:
            self.tasks.append(asyncio.create_task(self.synchronize(), name='meeting-context-feed'))
        for task in self.tasks:
            task.add_done_callback(self.background_finished)

    def background_finished(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            self.telemetry.emit('meeting_background_failed', task=task.get_name(),
                                error_type=type(task.exception()).__name__)
            log.error('meeting_background_failed', extra={'session_id': self.sid,
                                                        'error_type': type(task.exception()).__name__})

    def record(self, event_key: str, role: str, text: str, interrupted: bool = False,
               speaker: str | None = None) -> None:
        if not text.strip():
            return
        if len(text) > 20000:
            for index, offset in enumerate(range(0, len(text), 20000)):
                self.record(f'{event_key}:part:{index}', role, text[offset:offset + 20000],
                            interrupted=interrupted and offset + 20000 >= len(text), speaker=speaker)
            return
        event = {'event_key': event_key, 'role': role, 'text': text[:20000], 'interrupted': interrupted,
                 'observed_at': time.time()}
        if role == 'assistant' and speaker:
            event['speaker'] = speaker
        try:
            self.pending.put_nowait(event)
        except asyncio.QueueFull:
            self.telemetry.emit('conversation_queue_full')
            log.error('conversation_queue_full', extra={'session_id': self.sid})
            task = asyncio.create_task(self.journal(event))
            self.journal_tasks.add(task)
            task.add_done_callback(self.journal_finished)

    def journal_finished(self, task: asyncio.Task) -> None:
        self.journal_tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error('conversation_journal_failed', extra={'session_id': self.sid,
                                                          'error_type': type(task.exception()).__name__})

    async def journal(self, event: dict) -> None:
        import aiofiles
        async with self.journal_lock:
            await asyncio.to_thread(self.outbox.parent.mkdir, parents=True, exist_ok=True)
            async with aiofiles.open(self.outbox, 'a', encoding='utf-8') as file:
                await file.write(json.dumps(event, ensure_ascii=False) + '\n')
                await file.flush()

    # 408/429 are the only 4xx that can succeed on retry. Everything else is terminal:
    # retrying a 404 (deleted meeting) or a 401 (rotated key) parked the only outbox
    # reader forever, and once pending filled (256) every later record was diverted to
    # the journal and never replayed.
    _TRANSIENT_STATUSES = frozenset({408, 429})

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str) -> float:
        if retry_after.strip().isdigit():
            return min(30.0, max(1.0, float(retry_after)))
        return min(30.0, 2.0 ** min(attempt, 5))

    async def transmit(self, event: dict) -> None:
        attempt = 0
        while True:
            try:
                response = await self.http.post(self.url + '/conversation', headers=self.headers,
                                                json=event, timeout=4)
                if (400 <= response.status_code < 500
                        and response.status_code not in self._TRANSIENT_STATUSES):
                    # Terminal: the event stays on disk, only the cursor moves past it.
                    self.telemetry.emit('conversation_persistence_rejected',
                                        status=response.status_code)
                    log.error('conversation_persistence_rejected',
                              extra={'session_id': self.sid,
                                     'event_id': event.get('event_key'),
                                     'error_type': f'http_{response.status_code}'})
                    return
                response.raise_for_status()
                if attempt:
                    self.telemetry.emit('conversation_persistence_recovered', attempts=attempt)
                return
            except httpx.HTTPStatusError as error:
                delay = self._retry_delay(attempt, error.response.headers.get('Retry-After', ''))
            except httpx.HTTPError:
                delay = self._retry_delay(attempt, '')
            attempt += 1
            self.telemetry.emit('conversation_persistence_retry', attempts=attempt)
            await asyncio.sleep(delay)

    async def persist(self) -> None:
        # Disk persistence must never wait for the API to come back online.
        while True:
            event = await self.pending.get()
            try:
                while True:
                    try:
                        await self.journal(event)
                        break
                    except OSError:
                        self.telemetry.emit('conversation_journal_retry')
                        await asyncio.sleep(1)
            except asyncio.CancelledError:
                await self.journal(event)
                raise
            finally:
                self.pending.task_done()

    async def send(self) -> None:
        import aiofiles
        writer = asyncio.create_task(self.persist(), name='conversation-disk-writer')
        offsets = {}
        try:
            while True:
                paths = await asyncio.to_thread(
                    lambda: sorted(self.outbox.parent.glob(f'conversation-{self.sid}-*.jsonl')))
                for path in paths:
                    async with aiofiles.open(path, encoding='utf-8') as file:
                        await file.seek(offsets.get(path, 0))
                        while True:
                            line = await file.readline()
                            if not line or not line.endswith('\n'):
                                break  # An append in progress is retried, never discarded.
                            try:
                                event = json.loads(line)
                            except ValueError:
                                self.telemetry.emit('conversation_journal_corrupt_line')
                            else:
                                await self.transmit(event)
                            offsets[path] = await file.tell()
                await asyncio.sleep(.1)
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    async def apply(self, data: dict) -> None:
        self.visual_context = data.get('visual_context', {})
        gate = getattr(self.agent, 'gate', None)
        active_ids = [str(p['id']) for p in data['active_speakers']]
        if gate and active_ids:
            gate.set_reported_speaker(active_ids[0] if len(active_ids) == 1 else '__multiple__')
        # Thinking is already an active native response: playback has not started yet.
        # Keep the cursor unchanged so deferred annotations are fetched again.
        if (getattr(self.agent, 'user_speaking', False) or
                getattr(getattr(self.agent, 'turns', None), 'awaiting_transcript', False) or
                getattr(self.agent, 'is_speaking', False) or
                getattr(self.agent, 'voice_state', 'listening') != 'listening'):
            return
        messages = data['messages']
        roster = [{'id': p['id'], 'name': p['name']} for p in data['participants'] if p['present']]
        active = [{'id': p['id'], 'name': p['name']} for p in data['active_speakers']]
        if active:
            self.last_reported_speakers = active
        present_ids = {p['id'] for p in roster}
        self.last_reported_speakers = [p for p in self.last_reported_speakers if p['id'] in present_ids]
        # A brief speech-off/on for the same person is not a new identity update.
        visual_key = [(v['stream_id'], v['generation'], v['fresh'],
                       (v.get('cached_summary') or {}).get('source', {}).get('revision'))
                      for v in self.visual_context.get('screens', [])]
        fingerprint_data = [roster, self.last_reported_speakers]
        if self.visual_context:
            fingerprint_data.append(visual_key)
        fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True).encode()).hexdigest()
        # Native Gemini already receives user audio and its own output. Add only verified
        # Recall annotations; native assistant replies are already in model history.
        annotations = [{'speaker': m['speaker'], 'participant_id': m['participant_id'],
                        'text': m['text'][:2000]} for m in messages
                       if m['source'] == 'recall-live' and data['cursor'] > self.cursor]
        if fingerprint != self.fingerprint or annotations:
            payload = {'record_type': 'meeting_metadata_not_a_new_question',
                'participants': roster, 'recall_reported_last_speakers': self.last_reported_speakers,
                'attributed_transcript_updates': annotations,
                'transcript_policy': 'Alternative attribution of speech already heard, not additional turns. '
                                     'Do not answer these historical statements again.'}
            if self.visual_context:
                payload['cached_visual_reference_not_a_request'] = self.visual_context
            ctx = self.agent.chat_ctx.copy()
            ctx.add_message(role='user', id='meeting-metadata-' + uuid4().hex,
                content='Silent meeting context update. This is data, not a request to speak.\n' +
                        json.dumps(payload, ensure_ascii=False))
            await self.agent.update_chat_ctx(ctx)
            self.fingerprint = fingerprint
            self.telemetry.emit('meeting_context_updated', participants=len(roster),
                                reported_speakers=len(active), transcript_updates=len(annotations))
        self.cursor = data['cursor']

    async def synchronize(self) -> None:
        failed = False
        while True:
            try:
                response = await self.http.get(self.url + '/live-context', headers=self.headers,
                                               params={'after': self.cursor}, timeout=4)
                response.raise_for_status()
                await self.apply(response.json())
                if failed:
                    self.telemetry.emit('meeting_context_sync_recovered')
                failed = False
            except Exception as exc:
                if not failed:
                    self.telemetry.emit('meeting_context_sync_retry', error_type=type(exc).__name__)
                failed = True
            await asyncio.sleep(self.settings.context_poll_seconds)

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.pending.join(), timeout=5)
        except TimeoutError:
            self.telemetry.emit('conversation_shutdown_pending', count=self.pending.qsize())
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        while not self.pending.empty():
            await self.journal(self.pending.get_nowait())
            self.pending.task_done()
        await asyncio.gather(*self.journal_tasks, return_exceptions=True)
