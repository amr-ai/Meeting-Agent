import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.gating import ResponseGate
from app.agent.persona_switch import PersonaSwitch
from app.agent.personas import PERSONAS, persona_instructions, supported_voice
from app.agent.worker import build_instructions
from app.voice.recovery import RecoveringRealtimeSession


def caller(text='يا خالد هات مريم'):
    gate = ResponseGate(PERSONAS['khaled'].names, followup_until_stop=True)
    gate.transcript(text)
    turns = SimpleNamespace(input_id='one', last_input_epoch=gate.epoch, last_input_final=True,
                            last_input_text=text, last_input_at=time.monotonic(),
                            tool_started=Mock(), tool_completed=Mock())
    agent = SimpleNamespace(gate=gate, turns=turns, persona=PERSONAS['khaled'], persona_generation=0,
        persona_switching=False, instructions_for=lambda key: persona_instructions(key),
        update_instructions=AsyncMock(), native_tool_model=SimpleNamespace(change_persona=AsyncMock()),
        telemetry=SimpleNamespace(emit=Mock()), meeting_link=SimpleNamespace(record=Mock()))
    return agent, PersonaSwitch(agent)


def test_voice_catalogue_and_distinct_roles(settings):
    assert {p.name: p.voice for p in PERSONAS.values()} == {
        'Khaled': 'Iapetus', 'Mariam': 'Kore', 'Youssef': 'Charon', 'Adam': 'Puck'}
    assert all(supported_voice(p.voice) for p in PERSONAS.values())
    assert not supported_voice('Nika') and not supported_voice('Koda')
    for key, persona in PERSONAS.items():
        prompt = build_instructions(settings, 'meeting', key)
        assert f'You are {persona.name}' in prompt
        assert persona.character in prompt
        assert all(p.name in prompt for p in PERSONAS.values())
    assert 'You ARE the client' in persona_instructions('mariam')


async def test_repeated_switches_keep_gate_owner_and_context():
    a, controller = caller()
    context = object()
    a.chat_ctx = context
    a.gate.owner_id = 'participant-1'
    a.gate.set_reported_speaker('participant-1')
    for index, target in enumerate(('mariam', 'youssef', 'adam', 'khaled')):
        a.turns.input_id = str(index)
        a.turns.last_input_epoch = a.gate.epoch
        a.turns.last_input_at = time.monotonic()
        result = await controller.run(target, a.turns.last_input_text, True)
        assert result['switched']
        assert a.persona is PERSONAS[target] and a.chat_ctx is context
        assert a.gate.owner_id == 'participant-1'
        assert a.gate.has_wake_name(PERSONAS[target].name)
        assert a.gate.is_stop_request('اسكت يا ' + PERSONAS[target].names.split(',')[-1].removeprefix('يا '))
        a.gate.silence()
        assert not a.gate.tools_allowed()
        a.gate.set_reported_speaker('participant-1')
        a.gate.transcript(PERSONAS[target].name)
        assert a.gate.tools_allowed()
    assert a.native_tool_model.change_persona.await_count == 4


@pytest.mark.parametrize('text', ['مريم قالت الفكرة حلوة', 'يوسف كان هنا امبارح',
    'آدم عنده رأي تاني', 'متجيبش مريم', 'لو قلت هات يوسف هيحصل ايه؟'])
async def test_mentions_recommendations_and_negations_not_authorization(text):
    a, controller = caller('يا خالد ' + text)
    result = await controller.run('mariam', a.turns.last_input_text, False)
    assert not result['switched']
    a.native_tool_model.change_persona.assert_not_awaited()


async def test_stale_or_unrelated_tool_evidence_rejected():
    a, controller = caller()
    assert not (await controller.run('mariam', 'هات يوسف', True))['switched']
    a.turns.last_input_at -= 60
    assert not (await controller.run('mariam', a.turns.last_input_text, True))['switched']
    a.native_tool_model.change_persona.assert_not_awaited()


async def test_failure_keeps_original_identity_and_wake():
    a, controller = caller()
    a.native_tool_model.change_persona.side_effect = TimeoutError()
    result = await controller.run('mariam', a.turns.last_input_text, True)
    assert not result['switched'] and a.persona.key == 'khaled'
    assert a.gate.has_wake_name('خالد') and not a.gate.has_wake_name('مريم')
    assert not a.persona_switching


async def test_partial_command_and_revoked_permission_do_not_switch():
    a, controller = caller()
    a.turns.last_input_final = False
    assert not (await controller.run('mariam', a.turns.last_input_text, True))['switched']
    a.turns.last_input_final = True
    a.gate.silence()
    assert not (await controller.run('mariam', a.turns.last_input_text, True))['switched']
    a.native_tool_model.change_persona.assert_not_awaited()


async def test_buffer_cleared_and_agent_update_failure_restores_voice():
    a, controller = caller()
    audio = SimpleNamespace(clear_buffer=Mock())
    a.session = SimpleNamespace(output=SimpleNamespace(audio=audio))
    a.update_instructions.side_effect = [RuntimeError('update failed'), None]
    result = await controller.run('mariam', a.turns.last_input_text, True)
    assert not result['switched'] and a.persona.key == 'khaled'
    audio.clear_buffer.assert_called_once()
    assert [c.args[0] for c in a.native_tool_model.change_persona.await_args_list] == ['Kore', 'Iapetus']


async def test_connection_deadline_also_bounds_sdk_lock():
    session = object.__new__(RecoveringRealtimeSession)
    session._session_lock = asyncio.Lock()
    await session._session_lock.acquire()
    try:
        with pytest.raises(TimeoutError):
            await session._wait_persona_connection(object(), .02)
    finally:
        session._session_lock.release()


async def test_stop_during_switch_does_not_reauthorize_speech():
    a, controller = caller()
    async def transition(*args):
        a.gate.silence()
    a.native_tool_model.change_persona.side_effect = transition
    assert (await controller.run('mariam', a.turns.last_input_text, True))['switched']
    assert a.gate.silenced
    a.turns.tool_completed.assert_not_called()


async def test_concurrent_switch_rejected_and_duplicate_not_replayed():
    a, controller = caller()
    entered, release = asyncio.Event(), asyncio.Event()
    async def transition(*args):
        entered.set()
        await release.wait()
    a.native_tool_model.change_persona.side_effect = transition
    task = asyncio.create_task(controller.run('mariam', a.turns.last_input_text, True))
    await entered.wait()
    assert not (await controller.run('adam', a.turns.last_input_text, True))['switched']
    release.set()
    assert (await task)['switched']
    assert not (await controller.run('adam', a.turns.last_input_text, True))['switched']
    assert a.native_tool_model.change_persona.await_count == 1


async def test_transport_switch_replays_context_and_rolls_back_on_failure():
    # Exercise our adapter without opening a billable provider connection.
    session = object.__new__(RecoveringRealtimeSession)
    session._opts = SimpleNamespace(voice='Iapetus', instructions='old prompt')
    session._active_session = object()
    session._persona_rollback = None
    session._chat_ctx = object()
    history = session._chat_ctx
    session._mark_restart_needed = Mock()
    session._wait_persona_connection = AsyncMock(side_effect=[TimeoutError(), None])
    with pytest.raises(TimeoutError):
        await session.change_persona('Kore', 'new prompt')
    assert session._opts.voice == 'Iapetus' and session._opts.instructions == 'old prompt'
    assert session._chat_ctx is history and session._persona_rollback is None
    assert session._mark_restart_needed.call_count == 2
    session._wait_persona_connection = AsyncMock()
    await session.change_persona('Puck', 'Adam prompt')
    assert session._opts.voice == 'Puck' and session._chat_ctx is history
