import {Room, RoomEvent, Track, ParticipantKind} from 'https://cdn.jsdelivr.net/npm/livekit-client@2.21.0/dist/livekit-client.esm.mjs';
import {AudioLatencyProbe} from './latency.js';

// Only browser media and transport responsibilities live here.
export class AudioConnection {
  constructor(token, status, {measureLatency = false, onTrack = null, onSessionToken = null} = {}) {
    this.measureLatency = measureLatency; this.onTrack = onTrack; this.onSessionToken = onSessionToken;
    this.token = token; this.status = status; this.closed = false;
    this.room = new Room(); this.audio = document.querySelector('#audio');
    this.attached = null; this.retry = 0; this.timer = null;
  }
  async credentials() {
    const response = await fetch('/api/bridge/session', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:this.token})});
    if (!response.ok) {
      const error = new Error(`Session authorization failed (${response.status})`);
      error.terminal = [401,403,404,410].includes(response.status); throw error;
    }
    const data = await response.json(); this.token = data.bridge_token;
    this.agentIdentity = data.agent_identity; this.sessionId = data.session_id;
    try { this.onSessionToken?.(this.token); } catch { /* Storage may be unavailable. */ }
    this.scheduleRenewal(data.renew_after_seconds * 1000);
    return data;
  }
  scheduleRenewal(delay) {
    clearTimeout(this.timer);
    if (this.closed) return;
    this.timer = setTimeout(() => this.credentials().catch(error => {
      if (error.terminal) { this.status('Session authorization ended'); this.close(); }
      else { this.status('Session renewal delayed; voice connection retained'); this.scheduleRenewal(5000); }
    }), delay);
  }
  reportInputHealth(stage) {
    if (this.closed) return;
    const track = this.stream?.getAudioTracks()[0];
    const payload = new TextEncoder().encode(JSON.stringify({stage,
      muted:track?.muted === true, signal:this.probe?.lastInputEnergy > 0 && performance.now() - this.probe.lastInputEnergy < 5000}));
    try { this.room.localParticipant.publishData(payload, {reliable:true, topic:'input-audio-health'}).catch(() => {}); }
    catch { /* Diagnostics must not break capture or reconnection. */ }
  }
  recoverAgent(delay = 1000) {
    clearTimeout(this.agentTimer);
    if (this.closed) return;
    this.agentTimer = setTimeout(async () => {
      if (this.closed || this.room.remoteParticipants.has(this.agentIdentity)) return;
      try {
        // Ensure the existing room has a worker; keep its microphone connected.
        await this.credentials();
      } catch (error) {
        if (error.terminal) { this.status('Session authorization ended'); await this.close(); return; }
      }
      this.recoverAgent(Math.min(15000, delay * 2));
    }, delay);
  }
  reconcile() {
    for (const participant of this.room.remoteParticipants.values()) {
      for (const publication of participant.trackPublications.values()) {
        const selected = participant.identity === this.agentIdentity &&
          participant.kind === ParticipantKind.AGENT && publication.source === Track.Source.Microphone &&
          publication.kind === Track.Kind.Audio;
        publication.setSubscribed(selected);
        if (selected && publication.track) this.attach(publication.track);
      }
    }
  }
  async attach(track) {
    if (track === this.attached) return;
    this.attached?.detach(this.audio); this.attached = track; track.attach(this.audio);
    try { this.onTrack?.('output', track.mediaStreamTrack); } catch { /* Optional visual only. */ }
    try { this.probe?.watch('output', track.mediaStreamTrack); }
    catch { console.warn('Output audio observation unavailable'); }
    try { await this.room.startAudio(); await this.audio.play(); this.status('Listening — audio connected'); }
    catch { this.status('Audio playback was blocked. Reconnect with a user gesture.'); }
  }
  async connect() {
    this.status('Connecting'); const data = await this.credentials();
    if (this.measureLatency) try {
      this.probe = new AudioLatencyProbe(measurement => {
        fetch('/api/bridge/latency', {method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({token:this.token, ...measurement})})
          .then(response => { if (!response.ok) console.warn('Latency report was not accepted'); })
          .catch(() => console.warn('Latency report failed'));
      });
    } catch { console.warn('Audio energy latency measurement is unavailable'); }
    this.room.on(RoomEvent.DataReceived, (payload, participant, _kind, topic) => {
      if (topic !== 'voice-status' || participant?.identity !== this.agentIdentity ||
          participant.kind !== ParticipantKind.AGENT) return;
      try {
        const event = JSON.parse(new TextDecoder().decode(payload));
        if (event.error) this.status(event.error);
      } catch { /* Ignore malformed non-media messages. */ }
    });
    this.room.on(RoomEvent.TrackPublished, () => this.reconcile());
    this.room.on(RoomEvent.ParticipantConnected, () => this.reconcile());
    this.room.on(RoomEvent.TrackSubscribed, (_track, _publication, participant) => {
      if (participant.identity === this.agentIdentity) this.reconcile();
    });
    this.room.on(RoomEvent.ParticipantDisconnected, p => {
      if (p.identity === this.agentIdentity) {
        this.attached?.detach(this.audio); this.attached = null;
          this.status('Voice agent reconnecting; microphone retained');
          this.recoverAgent();
      }
    });
    this.room.on(RoomEvent.TrackUnsubscribed, track => {
      if (this.attached === track) { track.detach(this.audio); this.attached = null; }
    });
    this.room.on(RoomEvent.Reconnecting, () => this.status('Reconnecting'));
    this.room.on(RoomEvent.Reconnected, () => { this.retry = 0; this.reconcile(); });
    this.room.on(RoomEvent.Disconnected, () => { if (!this.closed) this.recover(); });
    this.audio.addEventListener('playing', () => console.info(JSON.stringify({
      event:'playback_element_playing', session_id:this.sessionId,
      clock_id:performance.timeOrigin, monotonic_ms:performance.now(),
      note:'Element playback event, not per-turn audible speech'})));
    await this.room.connect(data.server_url, data.participant_token, {autoSubscribe:false});
    if (this.closed) { await this.room.disconnect(); return; }
    await this.publishInput(); this.reconcile(); this.status('Listening — waiting for agent audio');
  }
  async publishInput() {
    if (this.publishing) return this.publishing;
    this.publishing = this.captureInput().finally(() => { this.publishing = null; });
    return this.publishing;
  }
  async captureInput() {
    if (this.closed) return;
    if (this.inputTrack) {
      await this.room.localParticipant.unpublishTrack(this.inputTrack, true).catch(() => {});
      this.inputTrack = null;
    }
    this.stream?.getTracks().forEach(t => t.stop());
    try { this.stream = await navigator.mediaDevices.getUserMedia({audio:true, video:false}); }
    catch (error) { this.reportInputHealth('capture_failed'); throw error; }
    if (this.closed) { this.stream.getTracks().forEach(t => t.stop()); return; }
    this.reportInputHealth('captured');
    const track = this.stream.getAudioTracks()[0];
    try { this.onTrack?.('input', track); } catch { /* Optional visual only. */ }
    try { this.probe?.watch('input', track); }
    catch { console.warn('Input audio observation unavailable'); }
    track.addEventListener('ended', () => {
      if (!this.closed) this.publishInput().catch(() => this.status('Meeting input track ended'));
    }, {once:true});
    const publication = await this.room.localParticipant.publishTrack(track,
      {name:'meeting-input', source:Track.Source.Microphone});
    this.inputTrack = publication.track;
    this.reportInputHealth('published');
    clearInterval(this.healthTimer);
    this.healthTimer = setInterval(() => this.reportInputHealth('energy'), 5000);
  }
  async recover() {
    if (this.recovering) return; this.recovering = true;
    try {
      while (!this.closed) {
        await new Promise(r => setTimeout(r, Math.min(1000 * 2 ** Math.min(this.retry++, 4), 15000)));
        try {
          const data = await this.credentials();
          if (this.closed) return;
          if (this.room.state !== 'connected') {
            await this.room.connect(data.server_url, data.participant_token, {autoSubscribe:false});
          }
          if (this.closed) { await this.room.disconnect(); return; }
          await this.publishInput(); this.reconcile(); this.retry = 0; return;
        } catch (error) {
          if (error.terminal) { this.status('Session authorization ended'); await this.close(); return; }
          this.status('Reconnection failed; retrying');
        }
      }

    } finally { this.recovering = false; }
  }
  async close() {
    this.closed = true; clearTimeout(this.timer); clearTimeout(this.agentTimer); clearInterval(this.healthTimer); this.attached?.detach(this.audio);
    this.probe?.close();
    this.stream?.getTracks().forEach(t => t.stop()); await this.room.disconnect();
    this.room.removeAllListeners();
  }
}
