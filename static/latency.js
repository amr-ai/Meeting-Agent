// Observes audio energy without changing or republishing either media stream.
export class AudioLatencyProbe {
  constructor(report) {
    this.report = report;
    this.context = new AudioContext({latencyHint:'interactive'});
    this.sink = this.context.createGain(); this.sink.gain.value = 0;
    this.sink.connect(this.context.destination);
    this.monitors = new Map(); this.inputSpeaking = false; this.outputSpeaking = false;
    this.lastInputEnergy = 0; this.lastOutputEnergy = 0; this.inputStarted = 0;
    this.pending = null; this.timer = setInterval(() => this.sample(), 20);
    this.context.resume().catch(() => {});
  }
  watch(kind, track) {
    const old = this.monitors.get(kind);
    old?.source.disconnect(); old?.analyser.disconnect();
    const source = this.context.createMediaStreamSource(new MediaStream([track]));
    const analyser = this.context.createAnalyser(); analyser.fftSize = 512;
    source.connect(analyser); analyser.connect(this.sink);
    this.monitors.set(kind, {source, analyser, buffer:new Float32Array(512)});
    this.context.resume().catch(() => {});
  }
  energy(kind) {
    const monitor = this.monitors.get(kind); if (!monitor) return 0;
    monitor.analyser.getFloatTimeDomainData(monitor.buffer);
    return Math.sqrt(monitor.buffer.reduce((sum, value) => sum + value * value, 0) / monitor.buffer.length);
  }
  sample() {
    if (this.context.state !== 'running') return;
    const now = performance.now();
    if (this.energy('input') > .012) {
      if (!this.inputSpeaking) this.inputStarted = now;
      this.inputSpeaking = true; this.lastInputEnergy = now;
    } else if (this.inputSpeaking && now - this.lastInputEnergy >= 300) {
      this.inputSpeaking = false;
      if (this.lastInputEnergy - this.inputStarted >= 100) {
        this.pending = {turn_id:crypto.randomUUID(), speech_end_ms:this.lastInputEnergy};
      }
    }
    if (this.energy('output') > .008) {
      if (!this.outputSpeaking && this.pending && !this.inputSpeaking) {
        const duration = now - this.pending.speech_end_ms;
        if (duration >= 0 && duration < 120000) this.report({
          ...this.pending, first_received_audio_ms:now,
          clock_origin_ms:performance.timeOrigin, duration_ms:Math.round(duration)});
        this.pending = null;
      }
      this.outputSpeaking = true; this.lastOutputEnergy = now;
    } else if (now - this.lastOutputEnergy > 300) this.outputSpeaking = false;
    if (this.pending && now - this.pending.speech_end_ms > 120000) this.pending = null;
  }
  close() {
    clearInterval(this.timer);
    for (const monitor of this.monitors.values()) monitor.source.disconnect();
    this.monitors.clear(); this.context.close().catch(() => {});
  }
}
