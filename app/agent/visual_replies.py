"""Schedule requested visual answers behind the ongoing voice conversation."""
import asyncio
import time


class VisualReplies:
    def __init__(self, agent, session):
        self.agent, self.session = agent, session
        self.tasks = set()
        self.closed = False

    def submit(self, call_id, question, stop_serial, epoch):
        if self.closed:
            return
        for task in self.tasks:
            task.cancel()  # Latest requested current snapshot supersedes an older visual answer.
        task = asyncio.create_task(self.deliver(question, stop_serial, epoch, call_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def valid(self, serial, epoch):
        gate = self.agent.gate
        return not self.closed and not gate.silenced and gate.stop_serial == serial and gate.epoch == epoch

    async def deliver(self, question, serial, epoch, call_id=''):
        started = time.monotonic()
        settings = getattr(self.agent, 'settings', None)
        budget = getattr(settings, 'visual_reply_seconds', 30)
        deadline, quiet_since = started + budget, None
        outcome = 'invalidated_or_deadline'
        self.agent.telemetry.emit('visual_reply_wait_started', call_id=call_id, epoch=epoch, stop_serial=serial)
        speech = waiter = None
        try:
            while self.valid(serial, epoch) and time.monotonic() < deadline:
                busy = (self.session.user_state == 'speaking' or self.agent.voice_state != 'listening'
                        or self.agent.turns.awaiting_transcript or not self.agent.input_available)
                quiet_since = None if busy else quiet_since or time.monotonic()
                if quiet_since and time.monotonic() - quiet_since >= .2:
                    self.agent.gate.authorize_background_reply()
                    self.agent.turns.awaiting_tool_reply = True
                    speech = self.session.generate_reply(instructions=
                        'Answer the pending screen question from the recently supplied visual input. '
                        'Do not repeat a progress message or call a screen tool again. Question: ' + question,
                        tool_choice='none', allow_interruptions=True)
                    self.agent.telemetry.emit('visual_reply_scheduled', call_id=call_id, wait_ms=(time.monotonic() - started) * 1000)
                    async def wait():
                        await asyncio.shield(speech)
                    waiter = asyncio.create_task(wait())
                    while self.valid(serial, epoch) and time.monotonic() < deadline:
                        try:
                            await asyncio.wait_for(asyncio.shield(waiter), .1)
                            failure = speech.exception()
                            outcome = 'failed' if failure else 'completed'
                            self.agent.telemetry.emit('visual_reply_finished', call_id=call_id, failed=failure is not None)
                            return
                        except TimeoutError:
                            pass
                    return
                await asyncio.sleep(.05)
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        except Exception as error:
            outcome = 'failed'
            self.agent.telemetry.emit('visual_reply_failed', call_id=call_id, error_type=type(error).__name__)
        finally:
            self.agent.telemetry.emit('visual_reply_terminal', call_id=call_id, outcome=outcome,
                                     elapsed_ms=(time.monotonic() - started) * 1000)
            if speech is not None and waiter is not None and not waiter.done():
                speech.interrupt()
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
