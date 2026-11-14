"""Fire-and-forget tasks must keep a reference and surface their failure."""
import asyncio

from app._tasks import running, spawn


async def test_spawn_keeps_a_reference_until_completion():
    gate = asyncio.Event()
    started = []

    async def work():
        started.append(True)
        await gate.wait()

    task = spawn(work(), name='unit-spawn')
    await asyncio.sleep(0)
    assert started == [True]
    assert running() >= 1
    gate.set()
    await task
    await asyncio.sleep(0)
    assert task.done()


async def test_spawn_retrieves_and_logs_the_exception(caplog):
    async def boom():
        raise ValueError('failed')

    task = spawn(boom(), name='unit-boom')
    with caplog.at_level('ERROR'):
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    assert any('background_task_failed' in record.message or
               getattr(record, 'error_type', '') == 'ValueError' or
               'unit-boom' in str(getattr(record, 'task', '')) for record in caplog.records), \
        'the failure must be logged, not left as an unretrieved task exception'


async def test_spawn_ignores_cancellation(caplog):
    async def forever():
        await asyncio.Event().wait()

    task = spawn(forever(), name='unit-cancel')
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not any('background_task_failed' in record.message for record in caplog.records)
