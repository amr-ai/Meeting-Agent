"""One compiled workflow per process, and a context cache the database itself invalidates."""
import pytest

from app.memory import context as context_module
from app.memory.context import decoded_utterances
from app.storage.models import Utterance
from app.workflows.chat_tools import ChatToolsWorkflow


async def _seed(store, sid, count, start=0.0):
    for index in range(count):
        await store.upsert_utterance(Utterance(
            meeting_id=sid, bot_id='bot-1', participant_id='p1', speaker='Khaled',
            text=f'line {index}', start=start + index, source='gemini-live',
            provider_event_id=f'{sid}-{index}'))


async def test_context_cache_invalidates_on_insert_and_on_finalisation(store, monkeypatch):
    db, sid = store
    context_module._DECODED.clear()
    await _seed(db, sid, 3)
    reads = []
    original = db.transcript_payloads

    async def counting(target_sid):
        reads.append(target_sid)
        return await original(target_sid)

    monkeypatch.setattr(db, 'transcript_payloads', counting)

    first = await decoded_utterances(db, sid)
    assert len(first) == 3 and len(reads) == 1

    again = await decoded_utterances(db, sid)
    assert len(again) == 3
    assert len(reads) == 1, 'a repeated read must not decode the table again'

    await _seed(db, sid, 1, start=3.0)
    after_insert = await decoded_utterances(db, sid)
    assert len(after_insert) == 4 and len(reads) == 2, 'a new utterance must invalidate'

    # Re-finalising an existing row changes the final count, not the row count.
    await db.execute('UPDATE utterances SET final=0 WHERE meeting_id=? AND start=0', (sid,))
    await decoded_utterances(db, sid)
    assert len(reads) == 3, 'a changed final flag must invalidate'


async def test_cache_is_bounded(store, monkeypatch):
    db, sid = store
    context_module._DECODED.clear()
    monkeypatch.setattr(context_module, '_DECODED_LIMIT', 2)
    await _seed(db, sid, 1)
    for name in ('a', 'b', 'c'):
        await db.create(name, 'meeting', 7200)
        await _seed(db, name, 1)
        await decoded_utterances(db, name)
    assert len(context_module._DECODED) == 2


async def test_one_workflow_instance_serves_many_meetings(store):
    db, sid = store
    await db.create('second-meeting', 'meeting', 7200)
    workflow = ChatToolsWorkflow(db, sender=None)

    first = await workflow.run('read_meeting_chat', {}, authorized=True, sid=sid)
    second = await workflow.run('read_meeting_chat', {}, authorized=True, sid='second-meeting')
    missing = await workflow.run('read_meeting_chat', {}, authorized=True, sid='absent')

    assert 'error' not in first and 'error' not in second
    assert missing == {'error': 'Meeting not found'}


async def test_workflow_still_rejects_unauthorized_and_unbound_meeting(store):
    db, _ = store
    workflow = ChatToolsWorkflow(db, sender=None)

    assert (await workflow.run('read_meeting_chat', {}))['error'] == \
        'No addressed voice/chat operation is authorized'
    # No sid bound anywhere: must not guess one.
    assert (await workflow.run('read_meeting_chat', {}, authorized=True))['error'] == \
        'Meeting not found'


async def test_graph_is_not_rebuilt_per_call(store):
    db, sid = store
    workflow = ChatToolsWorkflow(db, sender=None)
    graph = workflow.graph
    await workflow.run('read_meeting_chat', {}, authorized=True, sid=sid)
    assert workflow.graph is graph


@pytest.mark.parametrize('operation', ['send_meeting_chat', 'research_and_send_meeting_chat'])
async def test_sending_still_requires_an_idempotency_key(store, operation):
    db, sid = store
    workflow = ChatToolsWorkflow(db, sender=None)
    result = await workflow.run(operation, {'message': 'hi'}, authorized=True, sid=sid)
    assert 'idempotency key' in result['error']
