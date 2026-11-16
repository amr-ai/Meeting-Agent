"""Supervision must survive a failing store query; it owns expiry, leave and reconcile."""
import asyncio
from types import SimpleNamespace

import pytest

from app import main as main_module


class _Store:
    def __init__(self, failures):
        self.failures = failures
        self.row_calls = 0
        self.purges = []

    async def rows(self, sql, args=None):
        self.row_calls += 1
        if self.failures:
            self.failures -= 1
            raise OSError('database is locked')
        return []

    async def purge(self, days):
        self.purges.append(days)


def _app(store):
    return SimpleNamespace(state=SimpleNamespace(store=store, recall=SimpleNamespace(),
                                                 lk=SimpleNamespace(), monitor_errors=0))


async def test_a_failing_query_does_not_kill_supervision(monkeypatch, settings):
    monkeypatch.setattr(main_module, 'SUPERVISOR_INTERVAL_SECONDS', 0)
    store = _Store(failures=1)
    app = _app(store)

    task = asyncio.create_task(main_module.supervise(app, settings))
    try:
        for _ in range(500):
            await asyncio.sleep(.01)
            if store.purges:
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert app.state.monitor_errors == 1, 'the failed cycle must be counted'
    assert store.purges and set(store.purges) == {settings.retention_days}, \
        'the next cycle must run: expiry, leave, reconcile and purge stay alive'
    assert store.row_calls >= 2


async def test_cancellation_is_not_swallowed(monkeypatch, settings):
    monkeypatch.setattr(main_module, 'SUPERVISOR_INTERVAL_SECONDS', 0)
    task = asyncio.create_task(main_module.supervise(_app(_Store(failures=0)), settings))
    await asyncio.sleep(.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
