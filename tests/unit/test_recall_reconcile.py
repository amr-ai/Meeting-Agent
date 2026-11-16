"""An empty provider transcript must never erase stored meeting speech."""
import asyncio
from types import SimpleNamespace

import pytest

from app import main as main_module
from app.storage.models import Utterance
from app.storage.sqlite import Store


class _Recall:
    def __init__(self, final):
        self.final = final
        self.calls = 0

    async def status(self, bot_id):
        return {'status_changes': [{'code': 'done'}]}

    async def final_transcript(self, bot_id):
        self.calls += 1
        return self.final

    async def leave(self, bot_id):
        return None


class _LiveKit:
    async def ensure(self, meeting):
        return None

    async def cleanup(self, sid):
        return None


def _app(store, recall):
    return SimpleNamespace(state=SimpleNamespace(store=store, recall=recall, lk=_LiveKit(),
                                                 monitor_errors=0, empty_finals=set()))


async def _one_supervisor_cycle(app, settings):
    task = asyncio.create_task(main_module.supervise(app, settings))
    try:
        for _ in range(500):
            await asyncio.sleep(.01)
            if app.state.recall.calls:
                break
        await asyncio.sleep(.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _utterance(sid, text, start):
    return Utterance(meeting_id=sid, bot_id='bot-1', participant_id='p1', text=text,
                     start=start, source='gemini-live', provider_event_id=f'ev-{start}')


async def test_empty_final_transcript_keeps_speech_and_allows_later_inserts(tmp_path, settings):
    store = Store(tmp_path / 'reconcile.sqlite3')
    await store.open()
    sid = 'session-empty-final'
    await store.create(sid, 'meeting', 7200)
    await store.update(sid, 'active', 'bot-1')
    await store.upsert_utterance(_utterance(sid, 'What did we decide?', 1.0))
    recall = _Recall([])

    await _one_supervisor_cycle(_app(store, recall), settings)

    assert recall.calls == 1, 'supervisor must still ask the provider for a final transcript'
    rows = await store.rows('SELECT COUNT(*) AS count FROM utterances WHERE meeting_id=?', (sid,))
    assert rows[0]['count'] == 1, 'an empty artifact must not delete stored utterances'
    marker = await store.rows(
        "SELECT COUNT(*) AS count FROM memory WHERE meeting_id=? AND kind='reconciled'", (sid,))
    assert marker[0]['count'] == 0, 'an empty artifact must not write the blocking marker'

    # The real damage of the old behaviour was that this insert was refused afterwards.
    await store.upsert_utterance(_utterance(sid, 'We agreed on the API shape.', 2.0))
    rows = await store.rows('SELECT COUNT(*) AS count FROM utterances WHERE meeting_id=?', (sid,))
    assert rows[0]['count'] == 2
    await store.close()


async def test_missing_final_transcript_is_still_a_noop(tmp_path, settings):
    store = Store(tmp_path / 'pending.sqlite3')
    await store.open()
    sid = 'session-no-final'
    await store.create(sid, 'meeting', 7200)
    await store.update(sid, 'active', 'bot-1')
    await store.upsert_utterance(_utterance(sid, 'opening remark', 1.0))
    recall = _Recall(None)

    await _one_supervisor_cycle(_app(store, recall), settings)

    rows = await store.rows('SELECT COUNT(*) AS count FROM utterances WHERE meeting_id=?', (sid,))
    assert rows[0]['count'] == 1
    assert not await store.rows(
        "SELECT 1 FROM memory WHERE meeting_id=? AND kind='reconciled'", (sid,))
    await store.close()
