import asyncio
import json
from uuid import uuid4

import pytest

from app.meeting.events import EventProcessor, parse_utterance
from app.memory.archive import SpeechArchive, archive_path, archived_context
from app.memory.context import context
from tests.unit.test_storage import data


async def test_utf8_archive_preserves_verified_names_native_uncertainty_and_assistant(store):
    db, sid = store
    await db.add_conversation(sid, 'native-question', 'user', 'ما هو الـAPI؟')
    await db.add_conversation(sid, 'assistant-answer', 'assistant', 'واجهة للتواصل بين البرامج',
                              speaker='Khaled', source='agent-playout', interrupted=True)
    item = parse_utterance(sid, 'bot-1', data('ما هو الـAPI؟', 'Amr Mohamed'), 'recall-event')
    await db.upsert_utterance(item)
    await db.add_conversation(sid, 'recall:7:1.0', 'user', item.text,
                              speaker=item.speaker, participant_id='7', source='recall-live')
    await db.add_conversation(sid, 'control', 'control', 'Agent state: listening')
    archive = SpeechArchive(db)
    await archive.flush()
    records = [json.loads(line) for line in archive_path(db, sid).read_text(encoding='utf-8').splitlines()]
    assert len(records) == 3  # Recall's DB conversation mirror is not exported twice.
    by_id = {row['id']: row for row in records}
    assert by_id['recall:7:1.0']['speaker'] == 'Amr Mohamed'
    assert by_id['voice:native-question']['speaker'] == 'Unknown'
    assert by_id['voice:native-question']['attribution'] == 'pending Recall attribution'
    assert by_id['voice:assistant-answer']['interrupted']
    text = archive_path(db, sid, 'txt').read_text(encoding='utf-8')
    assert 'واجهة للتواصل' in text and 'Amr Mohamed' in text and 'Khaled' in text
    assert 'Agent state:' not in text
    assert not db.speech_dirty


async def test_late_verified_name_and_final_reconciliation_refresh_files(store):
    db, sid = store
    await db.upsert_utterance(parse_utterance(sid, 'bot-1', data('original', None), 'live'))
    archive = SpeechArchive(db)
    await archive.flush()
    assert 'Unknown' in archive_path(db, sid, 'txt').read_text(encoding='utf-8')
    processor = EventProcessor(db)
    await processor.ingest('participant', sid, {'event': 'participant_events.update', 'data': {
        'bot': {'id': 'bot-1'}, 'data': {'participant': {'id': 7, 'name': 'Amr'},
                                      'timestamp': {'relative': 10}}}})
    await processor.process((await db.rows('SELECT * FROM events'))[0])
    await archive.flush()
    records = [json.loads(line) for line in archive_path(db, sid).read_text(encoding='utf-8').splitlines()]
    assert records[0]['speaker'] == 'Amr'
    replacement = parse_utterance(sid, 'bot-1', data('corrected', 'Amr', 1.1), 'final', source='recall-final')
    await db.reconcile(sid, [replacement])
    await archive.flush()
    text = archive_path(db, sid, 'txt').read_text(encoding='utf-8')
    assert 'corrected' in text and ': original' not in text


async def test_restart_backfills_files_and_deletion_does_not_resurrect_them(store):
    db, sid = store
    await db.add_conversation(sid, 'a', 'assistant', 'Persisted earlier', speaker='Khaled')
    db.speech_dirty.clear()  # Simulate a process restart losing in-memory dirty hints.
    archive = SpeechArchive(db)
    await archive.start()
    await archive.close()
    assert archive_path(db, sid).exists()
    await db.cleanup_journals(sid)
    db.speech_dirty.add(sid)  # A stale queued export must not recreate deleted data.
    await archive.flush()
    assert not archive_path(db, sid).exists() and not archive_path(db, sid, 'txt').exists()


async def test_file_context_recovers_older_named_speech_without_repeating_current_rows(store):
    db, sid = store
    for index in range(11):
        await db.upsert_utterance(parse_utterance(sid, 'bot-1', data('earlier topic', 'Other', index + 1), f'early-{index}'))
    await db.upsert_utterance(parse_utterance(sid, 'bot-1', data('approved the old budget', 'Nadia', 12), 'first'))
    for index in range(25):
        await db.upsert_utterance(parse_utterance(sid, 'bot-1', data('later topic', 'Other', index + 13), f'later-{index}'))
    await SpeechArchive(db).flush()
    result = await context(db, sid, 'Nadia')  # Search by name, not a word in the old utterance.
    assert 'Earlier speech file' in result and 'Nadia' in result and 'approved the old budget' in result
    assert not await archived_context(db, sid, 'Nadia', exclude={'recall:7:12.0'})
    other = str(uuid4())
    assert not await archived_context(db, other, 'Nadia')
    assert len(await context(db, sid, 'Nadia', limit=1000)) <= 1000


async def test_archive_write_failure_retries_without_losing_durable_speech(store, monkeypatch):
    db, sid = store
    await db.add_conversation(sid, 'a', 'assistant', 'Saved in SQLite', speaker='Khaled')
    archive = SpeechArchive(db)
    original = archive.export

    async def fail(sid):
        raise OSError('synthetic disk failure')

    monkeypatch.setattr(archive, 'export', fail)
    await archive.flush()
    assert sid in db.speech_dirty and (await db.rows('SELECT text FROM conversation'))[0]['text'] == 'Saved in SQLite'
    monkeypatch.setattr(archive, 'export', original)
    await archive.flush()
    assert not db.speech_dirty and archive_path(db, sid).exists()


async def test_concurrent_new_speech_remains_dirty_until_next_snapshot(store, monkeypatch):
    db, sid = store
    await db.add_conversation(sid, 'first', 'assistant', 'First', speaker='Khaled')
    archive = SpeechArchive(db)
    captured, release = asyncio.Event(), asyncio.Event()
    original = archive.records

    async def paused(sid):
        records = await original(sid)
        captured.set()
        await release.wait()
        return records

    monkeypatch.setattr(archive, 'records', paused)
    flushing = asyncio.create_task(archive.flush())
    await captured.wait()
    await db.add_conversation(sid, 'second', 'assistant', 'Second', speaker='Khaled')
    release.set()
    await flushing
    assert sid in db.speech_dirty
    monkeypatch.setattr(archive, 'records', original)
    await archive.flush()
    assert 'Second' in archive_path(db, sid, 'txt').read_text(encoding='utf-8')


async def test_archive_file_name_cannot_escape_session_directory(store):
    db, _ = store
    with pytest.raises(ValueError):
        archive_path(db, '../outside')


async def test_cancelled_snapshot_is_flushed_on_shutdown(store, monkeypatch):
    db, sid = store
    await db.add_conversation(sid, 'last', 'assistant', 'Last spoken reply', speaker='Khaled')
    archive = SpeechArchive(db)
    original = archive.records
    ready = asyncio.Event()

    async def blocked(sid):
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(archive, 'records', blocked)
    task = asyncio.create_task(archive.flush())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sid in db.speech_dirty
    monkeypatch.setattr(archive, 'records', original)
    await archive.close()
    assert 'Last spoken reply' in archive_path(db, sid, 'txt').read_text(encoding='utf-8')
