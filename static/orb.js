// Read-only visual analysis of the existing tracks; never route or republish voice.
export class VoiceOrb {
  constructor() {
    this.monitors = new Map(); this.level = 0; this.closed = false;
    const Context = window.AudioContext || window.webkitAudioContext;
    this.context = new Context(); this.sink = this.context.createGain();
    this.sink.gain.value = 0; this.sink.connect(this.context.destination);
    this.context.resume().catch(() => {}); this.animate();
  }
  watch(kind, track) {
    if (this.closed) return;
    this.monitors.get(kind)?.source.disconnect();
    this.monitors.get(kind)?.analyser.disconnect();
    const source = this.context.createMediaStreamSource(new MediaStream([track]));
    const analyser = this.context.createAnalyser(); analyser.fftSize = 256;
    source.connect(analyser); analyser.connect(this.sink);
    this.monitors.set(kind, {source, analyser, buffer:new Float32Array(256)});
    this.context.resume().catch(() => {});
  }
  animate() {
    if (this.closed) return;
    let energy = 0;
    for (const {analyser, buffer} of this.monitors.values()) {
      analyser.getFloatTimeDomainData(buffer);
      let sum = 0; for (const value of buffer) sum += value * value;
      energy = Math.max(energy, Math.sqrt(sum / buffer.length));
    }
    const target = Math.min(1, Math.max(0, (energy - .004) * 12));
    this.level += (target - this.level) * (target > this.level ? .38 : .12);
    document.body.style.setProperty('--level', this.level.toFixed(3));
    document.body.style.setProperty('--orb-scale', (1 + this.level * .14).toFixed(3));
    this.frame = requestAnimationFrame(() => this.animate());
  }
  close() {
    this.closed = true; cancelAnimationFrame(this.frame);
    for (const {source, analyser} of this.monitors.values()) { source.disconnect(); analyser.disconnect(); }
    this.monitors.clear(); this.context.close().catch(() => {});
    document.body.style.setProperty('--level', 0); document.body.style.setProperty('--orb-scale', 1);
  }
}
