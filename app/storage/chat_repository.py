import hashlib
import json
import time
from uuid import uuid4

CHAT_SCHEMA = '''
CREATE TABLE IF NOT EXISTS meeting_chat_messages (
 id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
 bot_id TEXT NOT NULL, provider_event_id TEXT, participant_id TEXT, participant_name TEXT NOT NULL,
 content TEXT NOT NULL, direction TEXT NOT NULL, provider_timestamp TEXT, relative_timestamp REAL,
 event_time REAL NOT NULL, received_at REAL NOT NULL, source TEXT NOT NULL, metadata TEXT NOT NULL,
 processing_status TEXT NOT NULL, delivery_status TEXT NOT NULL, reply_to_message_id TEXT,
 idempotency_key TEXT, generated_reply TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, error TEXT,
 UNIQUE(meeting_id,idempotency_key));
CREATE INDEX IF NOT EXISTS chat_history ON meeting_chat_messages(meeting_id,event_time,id);
CREATE INDEX IF NOT EXISTS chat_work ON meeting_chat_messages(processing_status,next_attempt);
CREATE TABLE IF NOT EXISTS chat_threads (
 meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
 participant_id TEXT NOT NULL, last_reply REAL NOT NULL, last_event REAL NOT NULL,
 PRIMARY KEY(meeting_id,participant_id));
CREATE TABLE IF NOT EXISTS chat_voice_jobs (
 id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
 request_key TEXT NOT NULL, query TEXT NOT NULL, instructions TEXT NOT NULL,
 status TEXT NOT NULL, answer TEXT, result TEXT, error TEXT, created REAL NOT NULL,
 UNIQUE(meeting_id,request_key));
CREATE TABLE IF NOT EXISTS chat_metrics (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
 message_id TEXT, stage TEXT NOT NULL, duration_ms REAL NOT NULL, created REAL NOT NULL);
'''


class ChatRepository:
    def __init__(self, store):
        self.store = store

    async def metric(self, sid, mid, stage, duration):
        await self.store.execute('INSERT INTO chat_metrics(meeting_id,message_id,stage,duration_ms,created) VALUES (?,?,?,?,?)',
                                 (sid, mid, stage, duration, time.time()))

    async def receive(self, item: dict, self_names: set[str]) -> None:
        start = time.perf_counter()
        if item['participant_name'] == 'Unknown' and item['participant_id']:
            known = await self.store.rows('SELECT name,updated FROM participants WHERE meeting_id=? AND id=?',
                                          (item['meeting_id'], item['participant_id']))
            if known and known[0]['updated'] <= item['relative_timestamp']:
                item = {**item, 'participant_name': known[0]['name']}
        # Recall's documented participant schema has no is_bot flag. Suppress exact
        # outbound echoes, plus conservative configured-name collisions, not guessed flags.
        echoes = await self.store.rows('''SELECT id FROM meeting_chat_messages WHERE meeting_id=?
          AND direction='outgoing' AND content=? AND received_at BETWEEN ? AND ? LIMIT 1''',
          (item['meeting_id'], item['content'], item['event_time'] - 300, item['event_time'] + 300))
        self_message = bool(echoes) or item['participant_name'].casefold() in self_names
        metadata = {**item['metadata'], 'self_filter': 'outbox_echo' if echoes else
                    ('configured_name_collision' if self_message else None)}
        await self.store.execute('''INSERT OR IGNORE INTO meeting_chat_messages
          (id,meeting_id,bot_id,provider_event_id,participant_id,participant_name,content,direction,
           provider_timestamp,relative_timestamp,event_time,received_at,source,metadata,
           processing_status,delivery_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (item['id'], item['meeting_id'], item['bot_id'], item['provider_event_id'], item['participant_id'],
           item['participant_name'], item['content'], 'outgoing' if echoes else 'incoming',
           item['provider_timestamp'], item['relative_timestamp'], item['event_time'], item['received_at'],
           item['source'], json.dumps(metadata, ensure_ascii=False), 'self' if self_message else 'pending', 'observed'))
        if item['metadata'].get('artifact_event_id'):
            await self.store.execute("UPDATE meeting_chat_messages SET metadata=json_set(metadata,'$.artifact_event_id',?) WHERE id=?",
                                     (item['metadata']['artifact_event_id'], item['id']))
        if echoes:
            await self.store.execute("UPDATE meeting_chat_messages SET delivery_status='observed' WHERE id=?", (echoes[0]['id'],))
        await self.metric(item['meeting_id'], item['id'], 'persistence', (time.perf_counter() - start) * 1000)
        await self.metric(item['meeting_id'], item['id'], 'provider_timestamp_to_reception',
                          (item['received_at'] - item['event_time']) * 1000)

    async def history(self, sid, query='', participant='', since=None, until=None, limit=20):
        clauses, args = ['meeting_id=?'], [sid]
        # Bind every constraint; never let a model select a different meeting.
        if query:
            clauses.append('content LIKE ?')
            args.append('%' + query + '%')
        if participant:
            clauses.append('(participant_id=? OR participant_name LIKE ?)')
            args.extend([participant, '%' + participant + '%'])
        if since is not None:
            clauses.append('event_time>=?')
            args.append(since)
        if until is not None:
            clauses.append('event_time<=?')
            args.append(until)
        rows = await self.store.rows('''SELECT id,participant_id,participant_name,content,direction,
          provider_timestamp,relative_timestamp,event_time,received_at,source,delivery_status,
          processing_status,reply_to_message_id FROM meeting_chat_messages WHERE ''' +
          ' AND '.join(clauses) + ' ORDER BY event_time DESC,id DESC LIMIT ?', (*args, min(max(limit, 1), 50)))
        return list(reversed(rows))

    async def relevant(self, sid, query, limit=10):
        terms = sorted({word.casefold().strip('?,؟،.:!') for word in query.split()
                        if len(word) > 2} - {'khaled', 'khalid', 'خالد', 'please', 'ممكن', 'what', 'about'})[:12]
        if not terms:
            return []
        predicates = ' OR '.join('content LIKE ?' for _ in terms)
        return await self.store.rows('SELECT * FROM meeting_chat_messages WHERE meeting_id=? AND (' +
            predicates + ') ORDER BY event_time DESC LIMIT ?',
            (sid, *(f'%{word}%' for word in terms), min(max(limit, 1), 20)))

    async def outgoing(self, sid, bot_id, name, parts, key, reply_to):
        digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
        async with self.store.lock:
            try:
                await self.store.db.execute('BEGIN IMMEDIATE')
                if key.startswith('research:'):
                    job_cursor = await self.store.db.execute('SELECT status FROM chat_voice_jobs WHERE id=? AND meeting_id=?', (key[len('research:'):], sid))
                    job = await job_cursor.fetchone()
                    if job and job['status'] == 'cancelled':
                        raise ValueError('Research was cancelled before sending')
                for index, text in enumerate(parts):
                    item_key = f'{key}:{index}'
                    cursor = await self.store.db.execute('''SELECT content,metadata FROM meeting_chat_messages
                      WHERE meeting_id=? AND idempotency_key=?''', (sid, item_key))
                    existing = await cursor.fetchone()
                    if existing and json.loads(existing['metadata']).get('content_digest') != digest:
                        raise ValueError('Idempotency key already belongs to different content')
                    await self.store.db.execute('''INSERT OR IGNORE INTO meeting_chat_messages
                      (id,meeting_id,bot_id,participant_name,content,direction,event_time,received_at,
                       source,metadata,processing_status,delivery_status,reply_to_message_id,idempotency_key)
                      VALUES (?,?,?,?,?,'outgoing',?,?,?,?,'handled','queued',?,?)''',
                      (str(uuid4()), sid, bot_id, name, text, time.time(), time.time(), 'agent-chat',
                       json.dumps({'content_digest': digest, 'part': index, 'parts': len(parts), 'to': 'everyone'}),
                       reply_to, item_key))
                await self.store.db.commit()
            except BaseException:
                await self.store.db.rollback()
                raise
        return await self.store.rows('''SELECT * FROM meeting_chat_messages WHERE meeting_id=?
          AND idempotency_key IN (''' + ','.join('?' for _ in parts) + ') ORDER BY idempotency_key',
          (sid, *(f'{key}:{i}' for i in range(len(parts)))))

    async def enqueue_research(self, sid, query, instructions, key):
        meeting = await self.store.get(sid)
        if not meeting or meeting['mode'] != 'meeting' or not meeting['bot_id'] or meeting['status'] in {'done', 'left', 'failed', 'fatal'} or time.time() > meeting['expires']:
            raise ValueError('Research chat requires an active bound meeting')
        if not 2 <= len(query.strip()) <= 500:
            raise ValueError('Research query must contain 2 to 500 characters')
        existing = await self.store.rows('SELECT * FROM chat_voice_jobs WHERE meeting_id=? AND request_key=?', (sid, key))
        if existing and (existing[0]['query'] != query or existing[0]['instructions'] != instructions):
            raise ValueError('This research request key already belongs to different content')
        await self.store.execute('INSERT OR IGNORE INTO chat_voice_jobs(id,meeting_id,request_key,query,instructions,status,created) VALUES (?,?,?,?,?,?,?)',
                                 (str(uuid4()), sid, key, query, instructions, 'pending', time.time()))
        job = (await self.store.rows('SELECT id,status FROM chat_voice_jobs WHERE meeting_id=? AND request_key=?', (sid, key)))[0]
        return {'job_id': job['id'], 'status': job['status'], 'accepted': False,
                'note': 'Research is queued in the background. Briefly acknowledge starting it; do not claim it was sent. Results go to chat without blocking voice.'}

    async def recover(self):
        await self.store.execute("UPDATE chat_voice_jobs SET status='pending' WHERE status='running'")
        # Generation is replayable. A POST interrupted by process death is not.
        await self.store.execute("UPDATE meeting_chat_messages SET processing_status='pending' WHERE processing_status='generating'")
        await self.store.execute("UPDATE meeting_chat_messages SET delivery_status='unknown',error='restart_during_send' WHERE delivery_status='sending'")
