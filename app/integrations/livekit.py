import asyncio
import json
from datetime import timedelta

from livekit import api

from app.config.settings import Settings
from app.meeting.security import room_name


class LiveKitService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = api.LiveKitAPI(settings.livekit_url, settings.livekit_api_key,
                                    settings.livekit_api_secret.get_secret_value())
        self.lock = asyncio.Lock()

    async def ensure(self, meeting: dict) -> None:
        sid = meeting['id']
        async with self.lock:
            await self.client.room.create_room(api.CreateRoomRequest(name=room_name(sid),
                                                                     empty_timeout=60, max_participants=2))
            dispatches = await self.client.agent_dispatch.list_dispatch(room_name(sid))
            for d in dispatches:
                if d.agent_name != self.settings.agent_name or d.state.deleted_at:
                    continue
                if not d.state.jobs or any(j.state.status in (api.JobStatus.JS_PENDING,
                                                              api.JobStatus.JS_RUNNING)
                                          for j in d.state.jobs):
                    return
            await self.client.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
                room=room_name(sid), agent_name=self.settings.agent_name,
                metadata=json.dumps({'session_id': sid, 'mode': meeting['mode']})))

    def token(self, sid: str) -> str:
        return (api.AccessToken(self.settings.livekit_api_key,
                                self.settings.livekit_api_secret.get_secret_value())
                .with_identity(f'input-{sid}').with_name('Meeting audio input')
                .with_ttl(timedelta(minutes=10))
                .with_grants(api.VideoGrants(room_join=True, room=room_name(sid),
                    can_publish=True, can_subscribe=True, can_publish_data=True,
                    can_update_own_metadata=False, can_publish_sources=['microphone']))
                .to_jwt())

    async def cleanup(self, sid: str) -> None:
        try:
            await self.client.room.delete_room(api.DeleteRoomRequest(room=room_name(sid)))
        except api.TwirpError as exc:
            if exc.code != 'not_found':
                raise

    async def close(self) -> None:
        await self.client.aclose()
