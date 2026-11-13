import asyncio
import json
import logging
import time

from pydantic import SecretStr

from app.meeting.chat.mention_detector import MentionDetector
from app.meeting.chat.models import ENDED
from app.meeting.chat.responder import ChatReplyWorkflow
from app.meeting.chat.sender import ChatSender
from app.memory.context import context
from app.storage.chat_repository import ChatRepository
from app.workflows.research_chat import ResearchChatWorkflow

log = logging.getLogger(__name__)


class ChatService:
    def __init__(self, store, recall, http, settings, responder=None):
        self.store, self.recall, self.settings = store, recall, settings
        self.repo = ChatRepository(store)
        self.sender = ChatSender(store, recall, settings)
        self.detector = MentionDetector(settings.wake_names, settings.chat_followup_seconds)
        self.workflow = ChatReplyWorkflow(http, settings, responder)
        self.tasks: list[asyncio.Task] = []
        self.locks: dict[str, asyncio.Lock] = {}
        self.active: dict[str, asyncio.Task] = {}
        self.research_running: dict[str, asyncio.Task] = {}

    async def start(self):
        await self.repo.recover()
        self.tasks = [asyncio.create_task(self.run(), name='meeting-chat-responses'),
                      asyncio.create_task(self.outbox(), name='meeting-chat-outbox'),
                      asyncio.create_task(self.reconcile(), name='meeting-chat-recovery'),
                      asyncio.create_task(self.research_jobs(), name='voice-research-to-chat')]

    async def close(self):
        for task in [*self.tasks, *self.active.values(), *self.research_running.values()]:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.active.values(), *self.research_running.values(), return_exceptions=True)

    async def process(self, row):
        sid, mid = row['meeting_id'], row['id']
        async with self.locks.setdefault(sid, asyncio.Lock()):
            latest = await self.store.rows('SELECT * FROM meeting_chat_messages WHERE id=?', (mid,))
            if not latest or latest[0]['processing_status'] != 'pending':
                return
            row = latest[0]
            meeting = await self.store.get(sid)
            if (not meeting or meeting['status'] in ENDED or time.time() > meeting['expires'] or
                    row['source'] == 'recall-recovery' or time.time() - row['event_time'] > self.settings.chat_max_reply_age_seconds):
                await self.finish(mid, 'history_only')
                return
            if not self.settings.chat_auto_reply:
                await self.finish(mid, 'stored')
                return
            threads = await self.store.rows('SELECT * FROM chat_threads WHERE meeting_id=? AND participant_id=?',
                                           (sid, row['participant_id'])) if row['participant_id'] else []
            start = time.perf_counter()
            decision = self.detector.decide(row['content'], threads[0] if threads else None, row['event_time'])
            await self.repo.metric(sid, mid, 'mention_detection', (time.perf_counter() - start) * 1000)
            if decision not in {'direct', 'followup'}:
                await self.finish(mid, decision)
                return
            rate = await self.store.rows("""SELECT COUNT(*) AS total,
              SUM(CASE WHEN i.participant_id=? THEN 1 ELSE 0 END) AS participant_count
              FROM chat_metrics x JOIN meeting_chat_messages i ON i.id=x.message_id AND i.meeting_id=x.meeting_id
              WHERE x.meeting_id=? AND x.stage='generation_started' AND x.created>?""",
              (row['participant_id'], sid, time.time() - 60))
            if not row['generated_reply'] and (rate[0]['total'] >= self.settings.chat_auto_per_minute or
                    (rate[0]['participant_count'] or 0) >= self.settings.chat_participant_per_minute):
                await self.finish(mid, 'rate_limited')
                return
            await self.store.execute("UPDATE meeting_chat_messages SET processing_status='generating',attempts=attempts+1 WHERE id=?", (mid,))
            try:
                answer = row['generated_reply']
                if not answer:
                    await self.repo.metric(sid, mid, 'generation_started', 0)
                    # A bounded, on-demand union, never an audio-session injection.
                    relevant = await context(self.store, sid, row['content'], limit=10000)
                    start = time.perf_counter()
                    async with asyncio.timeout(35):
                        generated = await self.workflow.run_details({k: row[k] for k in
                            ('participant_id', 'participant_name', 'provider_timestamp')} |
                            {'content': row['content'][:6000], 'content_truncated': len(row['content']) > 6000}, relevant)
                    answer = generated['answer']
                    await self.repo.metric(sid, mid, 'text_generation', generated['generation_ms'])
                    await self.repo.metric(sid, mid, 'text_response_workflow', (time.perf_counter() - start) * 1000)
                    for name in type(self.settings).model_fields:
                        value = getattr(self.settings, name)
                        if isinstance(value, SecretStr) and len(value.get_secret_value()) >= 8:
                            answer = answer.replace(value.get_secret_value(), '[redacted]')
                    await self.store.execute('UPDATE meeting_chat_messages SET generated_reply=? WHERE id=?', (answer, mid))
                # Sending resolves the currently registered bot again and checks end state.
                result = await self.sender.send_chat_message(sid, answer, 'auto:' + mid, mid)
                await self.finish(mid, 'handled')
                if result['accepted'] and row['participant_id']:
                    await self.store.execute('''INSERT INTO chat_threads VALUES (?,?,?,?)
                      ON CONFLICT(meeting_id,participant_id) DO UPDATE SET
                        last_reply=excluded.last_reply,last_event=MAX(chat_threads.last_event,excluded.last_event)''',
                      (sid, row['participant_id'], time.time(), row['event_time']))
                await self.repo.metric(sid, mid, 'event_to_response_attempt', (time.time() - row['received_at']) * 1000)
            except asyncio.CancelledError:
                raise  # 'generating' is recovered on restart; the outbox is idempotent.
            except Exception as exc:
                await self.store.execute('''UPDATE meeting_chat_messages SET processing_status=?,
                  next_attempt=?,error=? WHERE id=?''',
                  ('pending' if row['attempts'] < 2 else 'failed', time.time() + 10,
                   type(exc).__name__, mid))
                log.warning('chat_response_failed', extra={'session_id': sid, 'event_id': mid,
                                                           'error_type': type(exc).__name__})

    async def finish(self, mid, status):
        await self.store.execute('UPDATE meeting_chat_messages SET processing_status=? WHERE id=?', (status, mid))

    async def run(self):
        while True:
            try:
                for sid, task in list(self.active.items()):
                    if task.done():
                        if not task.cancelled() and task.exception():
                            log.error('chat_worker_failed', extra={'session_id': sid,
                                'error_type': type(task.exception()).__name__})
                        del self.active[sid]
                rows = await self.store.rows('''SELECT * FROM meeting_chat_messages WHERE direction='incoming'
                  AND processing_status='pending' AND next_attempt<=? ORDER BY event_time LIMIT 100''', (time.time(),))
                for row in rows:
                    if row['meeting_id'] not in self.active and len(self.active) < self.settings.chat_workers:
                        self.active[row['meeting_id']] = asyncio.create_task(self.process(row))
            except Exception as exc:
                log.warning('chat_queue_retry', extra={'error_type': type(exc).__name__})
            await asyncio.sleep(.25)

    async def outbox(self):
        while True:
            try:
                await self.sender.drain()
            except Exception as exc:
                log.warning('chat_outbox_retry', extra={'error_type': type(exc).__name__})
            await asyncio.sleep(1)

    async def reconcile(self):
        # Provider artifacts support recovery after the call, not guaranteed live replay.
        while True:
            try:
                meetings = await self.store.rows("SELECT * FROM meetings WHERE bot_id IS NOT NULL AND status IN ('done','left')")
                for meeting in meetings:
                    sid = meeting['id']
                    marker = await self.store.rows("SELECT text FROM memory WHERE meeting_id=? AND kind='chat_reconciled'", (sid,))
                    if marker or not hasattr(self.recall, 'final_chat_events'):
                        continue
                    items = await self.recall.final_chat_events(meeting['bot_id'])
                    if items is None:
                        continue
                    from app.meeting.chat.models import parse_chat
                    for index, data in enumerate(items):
                        await self.repo.receive(parse_chat(sid, meeting['bot_id'], data,
                            f'recovery-{index}', source='recall-recovery'),
                            {self.settings.assistant_name.casefold()})
                    await self.store.execute('INSERT OR IGNORE INTO memory VALUES (?,?,?)', (sid, 'chat_reconciled', 'true'))
            except Exception as exc:
                log.warning('chat_reconciliation_retry', extra={'error_type': type(exc).__name__})
            await asyncio.sleep(60)

    async def research_jobs(self):
        workflow = ResearchChatWorkflow(self.store, self.sender, self.workflow)
        while True:
            try:
                for jid, task in list(self.research_running.items()):
                    if task.done():
                        if not task.cancelled():
                            task.exception()  # Retrieve failures; process_research records diagnostics.
                        del self.research_running[jid]
                jobs = await self.store.rows("SELECT * FROM chat_voice_jobs WHERE status IN ('pending','waiting') ORDER BY created LIMIT 100")
                for job in jobs:
                    if job['id'] not in self.research_running and len(self.research_running) < self.settings.chat_workers:
                        self.research_running[job['id']] = asyncio.create_task(self.process_research(job, workflow))
            except Exception as exc:
                log.warning('research_chat_queue_retry', extra={'error_type': type(exc).__name__})
            await asyncio.sleep(.5)

    async def cancel_research(self, sid, jid):
        async with self.store.lock:
            try:
                await self.store.db.execute('BEGIN IMMEDIATE')
                cursor = await self.store.db.execute('SELECT * FROM chat_voice_jobs WHERE meeting_id=? AND id=?', (sid, jid))
                job = await cursor.fetchone()
                if not job:
                    raise ValueError('Research task not found in this meeting')
                if job['status'] not in {'pending', 'running', 'waiting'}:
                    await self.store.db.commit()
                    return {'status': job['status'], 'cancelled': job['status'] == 'cancelled'}
                cursor = await self.store.db.execute("SELECT delivery_status FROM meeting_chat_messages WHERE meeting_id=? AND idempotency_key LIKE ?", (sid, 'research:' + jid + ':%'))
                deliveries = [r[0] for r in await cursor.fetchall()]
                if any(status in {'sending','accepted','observed','unknown'} for status in deliveries):
                    await self.store.db.commit()
                    return {'status': 'already_sent_or_in_flight', 'cancelled': False,
                            'note': 'A send may already have happened; no recall/cancellation is claimed'}
                await self.store.db.execute("UPDATE chat_voice_jobs SET status='cancelled' WHERE id=?", (jid,))
                await self.store.db.execute("UPDATE meeting_chat_messages SET delivery_status='cancelled' WHERE meeting_id=? AND idempotency_key LIKE ? AND delivery_status='queued'", (sid, 'research:' + jid + ':%'))
                await self.store.db.commit()
            except BaseException:
                await self.store.db.rollback()
                raise
        task = self.research_running.get(jid)
        if task:
            task.cancel()
        return {'status': 'cancelled', 'cancelled': True}

    async def process_research(self, job, workflow=None):
        workflow = workflow or ResearchChatWorkflow(self.store, self.sender, self.workflow)
        sid, jid = job['meeting_id'], job['id']
        async with self.store.lock:
            claimed = await self.store.db.execute("UPDATE chat_voice_jobs SET status='running' WHERE id=? AND status IN ('pending','waiting')", (jid,))
            await self.store.db.commit()
            if claimed.rowcount != 1:
                return
        try:
            state = await workflow.run(job)
            result = state.get('result', {})
            error = state.get('error')
            if error:
                current = await self.store.rows('SELECT status FROM chat_voice_jobs WHERE id=?', (jid,))
                status = 'cancelled' if current and current[0]['status'] == 'cancelled' else 'failed'
            elif result.get('accepted'):
                status = 'done'
            else:
                deliveries = {r['delivery_status'] for r in result.get('messages', [])}
                status = 'unknown' if 'unknown' in deliveries else ('failed' if deliveries & {'failed', 'cancelled'} else 'waiting')
            await self.store.execute('UPDATE chat_voice_jobs SET status=?,result=?,error=? WHERE id=?',
                                     (status, json.dumps(result), error, jid))
            if status != 'waiting':
                elapsed = (time.time() - job['created']) * 1000
                if job['request_key'].startswith('screen-research:') and elapsed >= 0:
                    await self.repo.metric(sid, jid, 'visual_research_created_to_terminal_wall', elapsed)
                label = 'accepted by Recall' if status == 'done' else (error or status)
                await self.store.add_conversation(sid, 'research-result-' + jid, 'control',
                    'Research-to-chat: ' + label + '.', source='chat-workflow')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.store.execute("UPDATE chat_voice_jobs SET status=CASE WHEN status='cancelled' THEN status ELSE 'failed' END,error=? WHERE id=?", (type(exc).__name__, jid))
            await self.store.add_conversation(sid, 'research-result-' + jid, 'control',
                'Research-to-chat failed; delivery was not confirmed. Check chat diagnostics.', source='chat-workflow')

    async def metrics(self, sid):
        return await self.store.rows('SELECT stage,COUNT(*) AS samples,AVG(duration_ms) AS mean_ms,MAX(duration_ms) AS max_ms FROM chat_metrics WHERE meeting_id=? GROUP BY stage', (sid,))
