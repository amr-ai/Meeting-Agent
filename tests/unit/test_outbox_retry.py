"""A terminal HTTP rejection must not park the conversation outbox forever."""
import httpx
import pytest

from app.memory.live import MeetingLink


class _Http:
    """Returns the queued responses in order and records every attempt."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, headers=None, json=None, **kwargs):
        self.calls.append(json)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _response(status, retry_after=None):
    headers = {'Retry-After': retry_after} if retry_after is not None else {}
    return httpx.Response(status, headers=headers,
                          request=httpx.Request('POST', 'http://api.test/conversation'))


def _link(settings, http):
    telemetry = type('_T', (), {'events': [], 'emit': lambda self, stage, **kw:
                                self.events.append((stage, kw))})()
    link = MeetingLink(agent=None, http=http, settings=settings, sid='s1', telemetry=telemetry)
    link._retry_delay = lambda attempt, retry_after: 0.0
    return link, telemetry


async def test_deleted_meeting_does_not_block_later_events(settings):
    http = _Http(_response(404), _response(200))
    link, telemetry = _link(settings, http)

    await link.transmit({'event_key': 'a', 'role': 'user', 'text': 'first'})
    await link.transmit({'event_key': 'b', 'role': 'user', 'text': 'second'})

    assert [call['event_key'] for call in http.calls] == ['a', 'b'], \
        'the rejected event must not be retried forever'
    assert ('conversation_persistence_rejected', {'status': 404}) in telemetry.events


async def test_rotated_key_is_terminal_but_rate_limit_is_retried(settings):
    http = _Http(_response(401), _response(429, '1'), _response(200))
    link, telemetry = _link(settings, http)

    await link.transmit({'event_key': 'a'})
    await link.transmit({'event_key': 'b'})

    assert [call['event_key'] for call in http.calls] == ['a', 'b', 'b']
    assert ('conversation_persistence_rejected', {'status': 401}) in telemetry.events
    assert ('conversation_persistence_retry', {'attempts': 1}) in telemetry.events


async def test_server_error_still_retries_until_success(settings):
    http = _Http(_response(503), _response(503), _response(200))
    link, telemetry = _link(settings, http)

    await link.transmit({'event_key': 'a'})

    assert len(http.calls) == 3
    assert ('conversation_persistence_recovered', {'attempts': 2}) in telemetry.events


@pytest.mark.parametrize('status', [400, 403, 410, 422])
async def test_other_terminal_statuses_return_immediately(settings, status):
    http = _Http(_response(status))
    link, telemetry = _link(settings, http)

    await link.transmit({'event_key': 'a'})

    assert len(http.calls) == 1
    assert ('conversation_persistence_rejected', {'status': status}) in telemetry.events
