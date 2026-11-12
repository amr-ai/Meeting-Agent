"""Background, rebuildable speech files; SQLite remains the durable source of truth."""
import asyncio
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from uuid import UUID, uuid4

log = logging.getLogger(__name__)


def archive_path(store, sid, extension='jsonl'):
    return store.path.parent / 'transcripts' / (str(UUID(sid)) + '.' + extension)


class SpeechArchive:
    def __init__(self, store):
        self.store = store
        self.task = None

    async def start(self):
        self.store.speech_dirty.update(row['id'] for row in await self.store.rows('SELECT id FROM meetings'))
        self.task = asyncio.create_task(self.run(), name='speech-file-archive')

    async def records(self, sid):
        rows = await self.store.rows('''SELECT c.*,p.name AS verified_name FROM conversation c
          LEFT JOIN participants p ON p.meeting_id=c.meeting_id AND p.id=c.participant_id
          WHERE c.meeting_id=? AND c.role IN ('user','assistant') AND c.source NOT IN ('recall-live','recall-final')
          ORDER BY c.seq''', (sid,))
        records = [{'id': 'voice:' + row['event_key'], 'speaker': row['verified_name'] or row['speaker'],
            'participant_id': row['participant_id'], 'role': row['role'], 'text': row['text'],
            'source': row['source'], 'time': row['created'], 'relative_seconds': None,
            'interrupted': bool(row['interrupted']), 'attribution':
            'assistant' if row['role'] == 'assistant' else ('verified' if row['participant_id'] and
                (row['verified_name'] or row['speaker']) != 'Unknown' else 'pending Recall attribution')}
            for row in rows]
        observed = await self.store.rows('''SELECT event_key,created FROM conversation
          WHERE meeting_id=? AND source IN ('recall-live','recall-final')''', (sid,))
        times = {row['event_key']: row['created'] for row in observed}
        names = {row['id']: row['name'] for row in await self.store.rows(
            'SELECT id,name FROM participants WHERE meeting_id=?', (sid,))}
        meeting = await self.store.get(sid)
        for item in await self.store.transcript(sid):
            if not item['final']:
                continue
            key = f"recall:{item['participant_id']}:{item['start']}"
            known = names.get(item['participant_id'])
            speaker = known if known and known != 'Unknown' else item['speaker']
            records.append({'id': key, 'speaker': speaker, 'participant_id': item['participant_id'],
                'role': 'user', 'text': item['text'], 'source': item['source'],
                'time': times.get(key), 'relative_seconds': item['start'], 'interrupted': False,
                'attribution': 'verified' if speaker != 'Unknown' else 'unresolved'})
        # Observed wall times and provider-relative times are different clocks.
        # Keep both and never present meeting creation as an exact speech timestamp.
        return sorted(records, key=lambda row: row['time'] if row['time'] is not None else
                      meeting['created'] + (row['relative_seconds'] or 0))

    async def export(self, sid):
        async with self.store.speech_file_lock:
            if sid in self.store.speech_deleted or not await self.store.get(sid):
                return
            records = await self.records(sid)
            path = archive_path(self.store, sid)

            def write():
                path.parent.mkdir(parents=True, exist_ok=True)
                structured = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records)
                readable = ['Meeting speech transcript: ' + sid,
                    'Recall supplies speaker identity. Live ASR pending attribution is a separate hypothesis, not another speaker.',
                    'UTC times are observation times; provider-relative speech times are labeled separately.', '']
                for row in records:
                    stamp = (datetime.fromtimestamp(row['time'], timezone.utc).isoformat()
                             if row['time'] is not None else 'observation time unavailable')
                    relative = (f"; speech +{row['relative_seconds']:.2f}s" if row['relative_seconds'] is not None else '')
                    text = row['text'].replace('\n', '\n    ')
                    readable.append(f"[{stamp}{relative}] {row['speaker']} [{row['source']}; {row['attribution']}]" +
                        (' [interrupted]' if row['interrupted'] else '') + ': ' + text)
                for target, content in [(path, structured), (path.with_suffix('.txt'), '\n'.join(readable) + '\n')]:
                    temporary = target.with_name(target.name + '.' + uuid4().hex + '.tmp')
                    try:
                        temporary.write_text(content, encoding='utf-8')
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)

            writing = asyncio.create_task(asyncio.to_thread(write))
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                # Do not release the deletion/write lock while its OS thread is active.
                await writing
                raise

    async def flush(self):
        for sid in list(self.store.speech_dirty):
            self.store.speech_dirty.discard(sid)
            try:
                await self.export(sid)
            except asyncio.CancelledError:
                self.store.speech_dirty.add(sid)
                raise
            except Exception as exc:
                self.store.speech_dirty.add(sid)
                log.warning('speech_archive_retry', extra={'session_id': sid, 'error_type': type(exc).__name__})

    async def run(self):
        while True:
            await self.flush()
            await asyncio.sleep(1)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.flush()


async def archived_context(store, sid, query='', exclude=(), limit=1500):
    path = archive_path(store, sid)

    def retrieve():
        recent = deque(maxlen=10)
        matches = []
        terms = {term.casefold() for term in query.split() if len(term) > 2}
        try:
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict) or not all(isinstance(row.get(key), str)
                            for key in ('id', 'speaker', 'source', 'attribution', 'text')):
                        continue
                    if row['id'] in exclude:
                        continue
                    text = f"{row['speaker']} [{row['source']}; {row['attribution']}]: {row['text']}"
                    score = sum(term in text.casefold() for term in terms)
                    if score:
                        matches.append((score, text))
                        matches.sort(key=lambda item: item[0], reverse=True)
                        del matches[10:]
                    recent.append(text)
        except FileNotFoundError:
            return ''
        except OSError as exc:
            log.warning('speech_archive_read_unavailable', extra={'error_type': type(exc).__name__})
            return ''
        chosen = [text for _, text in matches] if terms else list(recent)
        return '\n'.join(chosen)[:limit]

    return await asyncio.to_thread(retrieve)
