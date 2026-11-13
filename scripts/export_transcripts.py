"""Materialize speech files for sessions already stored locally; no provider calls."""
import asyncio

from app.config.settings import Settings
from app.memory.archive import SpeechArchive
from app.storage.sqlite import Store


async def main():
    store = Store(Settings().database_path)
    await store.open()
    archive = SpeechArchive(store)
    try:
        meetings = await store.rows('SELECT id FROM meetings')
        store.speech_dirty.update(row['id'] for row in meetings)
        await archive.flush()
        if store.speech_dirty:
            raise RuntimeError('Some transcript files could not be written; check archive diagnostics')
        print(f'Exported {len(meetings)} meeting speech archives to {store.path.parent / "transcripts"}')
    finally:
        await store.close()


if __name__ == '__main__':
    asyncio.run(main())
