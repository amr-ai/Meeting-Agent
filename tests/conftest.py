import base64
from uuid import uuid4

import pytest

from app.config.settings import Settings
from app.storage.sqlite import Store


@pytest.fixture(autouse=True)
def isolate_test_configuration(monkeypatch):
    # Worker import loads .env; tests must not inherit real account configuration.
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, admin_api_key='a' * 40, bridge_signing_secret='b' * 40,
        livekit_url='wss://example.livekit.cloud', livekit_api_key='test-key',
        livekit_api_secret='c' * 40, google_api_key='test-google',
        recall_api_key='test-recall', public_base_url='https://agent.example',
        recall_webhook_secret='whsec_' + base64.b64encode(b'secret-for-tests').decode(),
        database_path=tmp_path / 'test.sqlite3', latency_path=tmp_path / 'latency.jsonl')


@pytest.fixture
async def store(tmp_path):
    db = Store(tmp_path / 'store.sqlite3')
    await db.open()
    sid = str(uuid4())
    await db.create(sid, 'meeting', 7200)
    await db.update(sid, 'active', 'bot-1')
    yield db, sid
    await db.close()
