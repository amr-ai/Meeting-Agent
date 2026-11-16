import {AudioConnection} from '../media.js';
import {VoiceOrb} from '../orb.js';
const slot = 'meeting-voice-bridge-session';
let token = location.hash.slice(1);
try { token ||= sessionStorage.getItem(slot) || ''; if (token) sessionStorage.setItem(slot, token); } catch {}
history.replaceState(null, '', location.pathname);
let orb;
try { orb = new VoiceOrb(); } catch { /* The page still works without visual analysis. */ }
const setState = state => document.body.dataset.state = state;
const status = text => document.querySelector('#status').textContent = text;
const connection = new AudioConnection(token, status, {measureLatency:true,
  onTrack:(kind, track) => orb?.watch(kind, track), onSessionToken:value => {
  try { sessionStorage.setItem(slot, value); } catch {}
}});
if (!token) { status('Missing bridge session'); setState('error'); }
else connection.connect().then(() => setState('connected')).catch(async error => {
  status(error.message); setState(error.terminal ? 'error' : 'connecting');
  if (error.terminal) await connection.close();
  else await connection.recover();
});
window.addEventListener('pagehide', () => connection.close());
