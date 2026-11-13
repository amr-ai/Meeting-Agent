import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from uuid import uuid4


def runtime_revision(root: Path) -> str:
    """Hash deployed code, not .env, data, credentials or user content."""
    digest = hashlib.sha256()
    paths = list((root / 'app').rglob('*.py')) + list((root / 'prompts').glob('*.md'))
    paths += [root / 'requirements.lock', root / 'pyproject.toml']
    try:
        for path in sorted(paths):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b'\0')
            digest.update(path.read_bytes().replace(b'\r\n', b'\n'))
            digest.update(b'\0')
    except OSError:
        return 'unavailable'
    return digest.hexdigest()[:20]


class CapabilityRedaction(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if 'token=' in message:
            record.msg = re.sub(r'([?&]token=)[^&\s\"\x27]+', r'\1[redacted]', message)
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({'time': time.time(), 'level': record.levelname,
                           'event': record.getMessage(),
                           **{k: getattr(record, k) for k in ('session_id', 'event_id', 'error_type')
                              if hasattr(record, k)}})


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CapabilityRedaction())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in ('uvicorn.error', 'uvicorn.access'):
        logging.getLogger(name).addFilter(CapabilityRedaction())
    for name in ('httpx', 'httpcore', 'google_genai', 'livekit.plugins.google'):
        logging.getLogger(name).setLevel(logging.WARNING)


class Telemetry:
    def __init__(self, sid: str, path: Path):
        self.sid, self.path = sid, path
        self.clock_id = f'{os.getpid()}-{uuid4()}'
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=512)
        self.turn = str(uuid4())
        self.dropped = 0
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.write(), name='telemetry-writer')
        self.task.add_done_callback(self.writer_finished)

    def writer_finished(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logging.getLogger(__name__).error('telemetry_writer_failed',
                extra={'session_id': self.sid, 'error_type': type(task.exception()).__name__})

    def begin_turn(self) -> None:
        self.turn = str(uuid4())
        self.emit('transcribed_turn_started')

    def emit(self, stage: str, **data) -> None:
        row = {'session_id': self.sid, 'turn_id': self.turn, 'clock_id': self.clock_id,
               'stage': stage, 'monotonic': time.monotonic(), 'utc': time.time(), **data}
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1
            logging.getLogger(__name__).error('telemetry_queue_full')

    async def write(self) -> None:
        import aiofiles
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A separate file per worker process avoids concurrent JSONL writes.
        path = self.path.with_name(f'{self.path.stem}-{self.clock_id}{self.path.suffix}')
        async with aiofiles.open(path, 'a', encoding='utf-8') as stream:
            while (row := await self.queue.get()) is not None:
                await stream.write(json.dumps(row) + '\n')
                await stream.flush()

    async def close(self) -> None:
        if self.task:
            if self.task.done():
                await self.task  # Surface writer failures.
            else:
                await self.queue.put(None)
                await self.task
