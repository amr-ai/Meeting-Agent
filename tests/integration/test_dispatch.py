from types import SimpleNamespace
from uuid import uuid4

from app.integrations.livekit import LiveKitService


class FakeRoomApi:
    async def create_room(self, request):
        pass


class FakeDispatchApi:
    def __init__(self):
        self.dispatches = []
        self.created = 0

    async def list_dispatch(self, room):
        return self.dispatches

    async def create_dispatch(self, request):
        self.created += 1
        self.dispatches.append(SimpleNamespace(agent_name=request.agent_name,
            state=SimpleNamespace(deleted_at=0, jobs=[])))


async def test_dispatch_reuses_pending_and_replaces_finished_jobs(settings):
    import asyncio

    from livekit import api
    service = LiveKitService(settings)
    await service.close()
    dispatch = FakeDispatchApi()
    service.client = SimpleNamespace(room=FakeRoomApi(), agent_dispatch=dispatch)
    meeting = {'id': str(uuid4()), 'mode': 'meeting'}
    await asyncio.gather(service.ensure(meeting), service.ensure(meeting))
    assert dispatch.created == 1
    dispatch.dispatches[0].state.jobs = [SimpleNamespace(
        state=SimpleNamespace(status=api.JobStatus.JS_SUCCESS))]
    await service.ensure(meeting)
    assert dispatch.created == 2
