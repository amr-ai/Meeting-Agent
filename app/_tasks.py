"""Fire-and-forget task helper with a strong reference and a retrieved exception.

A bare ``asyncio.create_task`` keeps no reference, so the task can be garbage
collected mid-flight, and nothing ever reads its exception: the failure is reported
only as "Task exception was never retrieved" at collection time.
"""
import asyncio
import logging

log = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def spawn(coro, *, name: str | None = None) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)

    def _finished(done: asyncio.Task) -> None:
        _tasks.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            log.error('background_task_failed', extra={'task': done.get_name(),
                                                      'error_type': type(error).__name__})

    task.add_done_callback(_finished)
    return task


def running() -> int:
    """Number of live spawn() tasks; used by diagnostics and tests."""
    return len(_tasks)
