import {AudioConnection} from '../media.js';
const slot = 'meeting-voice-bridge-session';
let token = location.hash.slice(1);
try { token ||= sessionStorage.getItem(slot) || ''; if (token) sessionStorage.setItem(slot, token); } catch {}
history.replaceState(null, '', location.pathname);
const status = text => document.querySelector('#status').textContent = text;
const connection = new AudioConnection(token, status, {measureLatency:true, onSessionToken:value => {
  try { sessionStorage.setItem(slot, value); } catch {}
}});
if (!token) status('Missing bridge session');
else connection.connect().catch(async error => {
  status(error.message);
  if (error.terminal) await connection.close();
  else await connection.recover();
});
window.addEventListener('pagehide', () => connection.close());
