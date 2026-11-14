import asyncio
import json
import time
from pathlib import Path
from uuid import UUID

import aiosqlite

from app.storage.chat_repository import CHAT_SCHEMA
from app.storage.models import Utterance


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.speech_dirty: set[str] = set()
        self.speech_deleted: set[str] = set()
        self.speech_file_lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS meetings (
          id TEXT PRIMARY KEY, bot_id TEXT UNIQUE, mode TEXT NOT NULL,
          status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
          payload TEXT NOT NULL, processed INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
          error TEXT);
        CREATE TABLE IF NOT EXISTS utterances (
          meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
          participant_id TEXT NOT NULL, start REAL NOT NULL, final INTEGER NOT NULL,
          source TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(meeting_id, participant_id, start));
        CREATE TABLE IF NOT EXISTS participants (
          meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
          id TEXT NOT NULL, name TEXT NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(meeting_id,id));
        CREATE TABLE IF NOT EXISTS memory (
          meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(meeting_id,kind,text));
        CREATE TABLE IF NOT EXISTS participant_state (
          meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
          id TEXT NOT NULL, present INTEGER NOT NULL, speaking INTEGER NOT NULL,
          membership_stamp REAL NOT NULL, speech_stamp REAL NOT NULL, observed REAL NOT NULL,
          PRIMARY KEY(meeting_id,id));
        CREATE TABLE IF NOT EXISTS conversation (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
          event_key TEXT NOT NULL, role TEXT NOT NULL, speaker TEXT NOT NULL,
          participant_id TEXT, text TEXT NOT NULL, source TEXT NOT NULL,
          interrupted INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
          UNIQUE(meeting_id,event_key));
        CREATE INDEX IF NOT EXISTS conversation_session ON conversation(meeting_id,seq);
        CREATE TABLE IF NOT EXISTS bridge_metrics (
          meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
          turn_id TEXT NOT NULL, duration_ms REAL NOT NULL, payload TEXT NOT NULL,
          created REAL NOT NULL, PRIMARY KEY(meeting_id,turn_id));
        ''')
        await self.db.executescript(CHAT_SCHEMA)
        async with self.db.execute('PRAGMA table_info(events)') as cursor:
            columns = {r[1] for r in await cursor.fetchall()}
        if 'received_at' not in columns:
            await self.db.execute('ALTER TABLE events ADD COLUMN received_at REAL')
        await self.db.commit()

    async def close(self) -> None:
        await self.db.close()

    async def execute(self, sql: str, args: tuple = ()) -> None:
        async with self.lock:
            await self.db.execute(sql, args)
            await self.db.commit()

    async def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self.lock:
            async with self.db.execute(sql, args) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def create(self, sid: str, mode: str, lifetime: int) -> dict:
        await self.execute('INSERT INTO meetings VALUES (?,NULL,?,?,?,?)',
                           (sid, mode, 'starting', time.time(), time.time() + lifetime))
        return await self.get(sid)

    async def get(self, sid: str) -> dict | None:
        rows = await self.rows('SELECT * FROM meetings WHERE id=?', (sid,))
        return rows[0] if rows else None

    async def update(self, sid: str, status: str, bot_id: str | None = None) -> None:
        await self.execute('UPDATE meetings SET status=?,bot_id=COALESCE(?,bot_id) WHERE id=?',
                           (status, bot_id, sid))

    async def ingest(self, eid: str, sid: str, payload: dict) -> None:
        await self.execute('INSERT OR IGNORE INTO events(id,meeting_id,payload,received_at) VALUES (?,?,?,?)',
                           (eid, sid, json.dumps(payload), time.time()))

    async def upsert_utterance(self, item: Utterance) -> None:
        # A late partial may never overwrite a final; post-meeting data wins over live finals.
        await self.execute('''INSERT INTO utterances SELECT ?,?,?,?,?,?
        WHERE NOT EXISTS (SELECT 1 FROM memory WHERE meeting_id=? AND kind='reconciled')
        ON CONFLICT(meeting_id,participant_id,start) DO UPDATE SET
          final=excluded.final,source=excluded.source,payload=excluded.payload
        WHERE (excluded.final > utterances.final) OR
          (excluded.final = utterances.final AND
           (utterances.source != 'recall-final' OR excluded.source = 'recall-final'))''',
            (item.meeting_id, item.participant_id, item.start, int(item.final),
             item.source, item.model_dump_json(), item.meeting_id))
        self.speech_dirty.add(item.meeting_id)

    async def transcript(self, sid: str) -> list[dict]:
        rows = await self.rows('SELECT payload FROM utterances WHERE meeting_id=? ORDER BY start', (sid,))
        return [json.loads(row['payload']) for row in rows]

    async def transcript_payloads(self, sid: str) -> list[str]:
        """Undecoded payloads, so the caller can decode off the event loop."""
        rows = await self.rows('SELECT payload FROM utterances WHERE meeting_id=? ORDER BY start', (sid,))
        return [row['payload'] for row in rows]

    async def transcript_stats(self, sid: str) -> tuple[int, float, int]:
        """Cheap invalidation key for a cached decoded transcript: inserted, re-finalised."""
        rows = await self.rows('''SELECT COUNT(*) AS count, COALESCE(MAX(start),0) AS last,
          COALESCE(SUM(final),0) AS finals FROM utterances WHERE meeting_id=?''', (sid,))
        row = rows[0]
        return row['count'], row['last'], row['finals']

    async def add_conversation(self, sid: str, event_key: str, role: str, text: str,
                               speaker: str = 'Unknown', participant_id: str | None = None,
                               source: str = 'gemini-live', interrupted: bool = False,
                               created: float | None = None) -> None:
        await self.execute('''INSERT OR IGNORE INTO conversation
          (meeting_id,event_key,role,speaker,participant_id,text,source,interrupted,created)
          VALUES (?,?,?,?,?,?,?,?,?)''', (sid, event_key, role, speaker, participant_id,
          text, source, int(interrupted), created if created is not None else time.time()))
        if role in {'user', 'assistant'}:
            self.speech_dirty.add(sid)

    async def live_context(self, sid: str, after: int = 0) -> dict:
        participants = await self.rows('''SELECT p.id,p.name,COALESCE(s.present,1) AS present,
          COALESCE(s.speaking,0) AS speaking,s.observed FROM participants p
          LEFT JOIN participant_state s ON s.meeting_id=p.meeting_id AND s.id=p.id
          WHERE p.meeting_id=? ORDER BY p.id''', (sid,))
        # A stale platform timeline is not proof of who is speaking now.
        speakers = [p for p in participants if p['present'] and p['speaking'] and
                    p['observed'] is not None and time.time() - p['observed'] < 60]
        messages = await self.rows('''SELECT * FROM conversation WHERE meeting_id=? AND seq>?
                                   ORDER BY seq LIMIT 100''', (sid, after))
        return {'participants': participants, 'active_speakers': speakers,
                'messages': messages, 'cursor': messages[-1]['seq'] if messages else after}

    async def reconcile(self, sid: str, items: list[Utterance]) -> None:
        # Replace segmentation atomically: final resources can split live utterances differently.
        async with self.lock:
            try:
                await self.db.execute('BEGIN IMMEDIATE')
                await self.db.execute('DELETE FROM utterances WHERE meeting_id=?', (sid,))
                await self.db.executemany('INSERT INTO utterances VALUES (?,?,?,?,?,?)', [
                    (sid, u.participant_id, u.start, 1, 'recall-final', u.model_dump_json()) for u in items])
                await self.db.execute('INSERT OR IGNORE INTO memory VALUES (?,?,?)',
                                      (sid, 'reconciled', 'true'))
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        self.speech_dirty.add(sid)

    async def purge(self, retention_days: int) -> None:
        expired = await self.rows('SELECT id FROM meetings WHERE created < ? AND status IN (?,?,?)',
                           (time.time() - retention_days * 86400, 'done', 'left', 'failed'))
        for row in expired:
            await self.cleanup_journals(row['id'])
        await self.execute('DELETE FROM meetings WHERE created < ? AND status IN (?,?,?)',
                           (time.time() - retention_days * 86400, 'done', 'left', 'failed'))

    async def cleanup_journals(self, sid: str) -> None:
        canonical = str(UUID(sid))
        parent = self.path.parent.resolve()

        def remove() -> None:
            for path in parent.glob(f'conversation-{canonical}-*.jsonl'):
                if path.resolve().parent == parent:
                    path.unlink(missing_ok=True)

        async with self.speech_file_lock:
            self.speech_deleted.add(canonical)
            self.speech_dirty.discard(canonical)
            await asyncio.to_thread(remove)
            for extension in ('jsonl', 'txt'):
                path = parent / 'transcripts' / (canonical + '.' + extension)
                await asyncio.to_thread(path.unlink, missing_ok=True)
