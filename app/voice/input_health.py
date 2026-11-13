"""Bounded metadata-only input diagnostics; never record or gate audio."""
import json
import time


class InputHealth:
    def __init__(self, agent, link):
        self.agent, self.link = agent, link
        self.frames = 0
        self.last_report = 0
        self.signal_seen = False
        self.interval_signal = False
        self.pcm_seen = False

    def connection(self, available):
        self.agent.input_available = available
        self.agent.telemetry.emit('meeting_input_connection', connected=available)
        self.link.record('input-connection-' + str(time.monotonic_ns()), 'control',
                         'Meeting audio participant connected; waiting for PCM.' if available else
                         'Meeting audio temporarily disconnected; keeping voice session alive for reconnection.')
        if not available:
            self.signal_seen = False
            self.pcm_seen = False

    def observe(self, frame):
        self.frames += 1
        if not self.pcm_seen:
            self.pcm_seen = True
            self.link.record('input-pcm-' + str(self.frames), 'control', 'Meeting audio PCM is reaching the voice worker.')
        now = time.monotonic()
        if self.frames == 1 or self.frames % 5 == 0:
            samples = frame.data if frame.data.format == 'h' else frame.data.cast('h')
            peak = max((abs(value) for value in samples), default=0)
            self.interval_signal |= peak > 256
            if peak > 256 and not self.signal_seen:
                self.signal_seen = True
                self.link.record('input-signal-' + str(self.frames), 'control', 'Audio signal detected at the voice worker.')
        if now - self.last_report >= 5:
            self.agent.telemetry.emit('worker_input_audio', frames=self.frames, signal=self.interval_signal)
            self.interval_signal = False
            self.last_report = now

    def browser(self, payload):
        try:
            event = json.loads(payload)
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict) or event.get('stage') not in {'captured', 'published', 'capture_failed', 'energy'}:
            return
        stage = event['stage']
        self.agent.telemetry.emit('browser_input_audio', stage_name=stage,
                                  signal=event.get('signal') is True, muted=event.get('muted') is True)
        if stage != 'energy':
            self.link.record('browser-input-' + str(time.monotonic_ns()), 'control',
                             'Meeting audio bridge: ' + stage)
