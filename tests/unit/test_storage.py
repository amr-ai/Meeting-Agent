from app.meeting.events import EventProcessor, parse_utterance


def data(text='hello', name='Alice', start=1.0):
    return {'participant': {'id': 7, 'name': name}, 'words': [
        {'text': text, 'start_timestamp': {'relative': start}, 'end_timestamp': {'relative': start + 1}}]}


async def test_duplicate_out_of_order_final_and_unknown_speaker(store):
    db, sid = store
    final = parse_utterance(sid, 'bot-1', data(name=None), 'event-final')
    assert final.speaker == 'Unknown'
    await db.upsert_utterance(final)
    await db.upsert_utterance(final)
    late_partial = final.model_copy(update={'text': 'incorrect partial', 'final': False})
    await db.upsert_utterance(late_partial)
    saved = await db.transcript(sid)
    assert len(saved) == 1 and saved[0]['text'] == 'hello' and saved[0]['final']


async def test_reconciliation_replaces_changed_segmentation_atomically(store):
    db, sid = store
    await db.upsert_utterance(parse_utterance(sid, 'bot-1', data(), 'live'))
    final = [parse_utterance(sid, 'bot-1', data('correct', start=1.1), 'final', source='recall-final')]
    await db.reconcile(sid, final)
    await db.reconcile(sid, final)
    assert [u['text'] for u in await db.transcript(sid)] == ['correct']


async def test_durable_inbox_and_idempotent_processing(store):
    db, sid = store
    event = {'event': 'transcript.data', 'data': {'bot': {'id': 'bot-1'}, 'data': data()}}
    processor = EventProcessor(db, 1)
    await processor.ingest('same-event', sid, event)
    await processor.ingest('same-event', sid, event)
    rows = await db.rows('SELECT * FROM events')
    assert len(rows) == 1
    # Simulate recovery with a fresh processor and no in-memory wakeups.
    recovered = EventProcessor(db, 1)
    await recovered.process(rows[0])
    await recovered.process(rows[0])
    assert len(await db.transcript(sid)) == 1
    assert (await db.rows('SELECT processed FROM events'))[0]['processed'] == 1


async def test_failed_reconciliation_keeps_old_transcript(store):
    db, sid = store
    item = parse_utterance(sid, 'bot-1', data(), 'old')
    await db.upsert_utterance(item)
    try:
        await db.reconcile(sid, [item, item])
    except Exception:
        pass
    assert (await db.transcript(sid))[0]['provider_event_id'] == 'old'


async def test_verified_participant_events_resolve_unknown_names_and_reject_stale_updates(store):
    db, sid = store
    await db.upsert_utterance(parse_utterance(sid, 'bot-1', data(name=None), 'utterance'))
    processor = EventProcessor(db)
    for eid, name, timestamp in [('new-name', 'Alice', 10), ('stale-name', 'Old name', 1)]:
        await processor.ingest(eid, sid, {'event': 'participant_events.update', 'data': {
            'bot': {'id': 'bot-1'}, 'data': {'participant': {'id': 7, 'name': name},
                                           'timestamp': {'relative': timestamp}}}})
        row = (await db.rows('SELECT * FROM events WHERE id=?', (eid,)))[0]
        await processor.process(row)
    assert (await db.transcript(sid))[0]['speaker'] == 'Alice'
    assert (await db.rows('SELECT name FROM participants'))[0]['name'] == 'Alice'
