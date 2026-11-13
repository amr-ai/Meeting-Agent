import {AudioConnection} from './media.js';
import {VoiceOrb} from './orb.js';
let connection, sessionId, sessionToken, orb;
const connect = document.querySelector('#connect'), disconnect = document.querySelector('#disconnect');
const headers = {'Content-Type':'application/json', 'X-Voice-UI':'1'};
const state = value => { document.body.dataset.state = value; };
const status = text => { document.querySelector('#status').textContent = text; };
async function stop() {
  state('stopping'); connect.disabled = true; disconnect.disabled = true;
  const token = connection?.token || sessionToken;
  let cleanupFailed = false;
  try { await connection?.close(); }
  finally {
    orb?.close(); orb = null;
    if (sessionId && token) {
      try {
        const response = await fetch('/api/ui/leave', {method:'POST', headers, body:JSON.stringify({token})});
        cleanupFailed = !response.ok;
      } catch { cleanupFailed = true; }
    }
    sessionId = null; sessionToken = null; connection = null;
    connect.disabled = false; disconnect.disabled = true;
    connect.querySelector('span').textContent = 'Start conversation';
    state(cleanupFailed ? 'error' : 'idle');
    status(cleanupFailed ? 'Disconnected. Server cleanup is pending.' : 'Ready when you are');
  }
}
connect.onclick = async () => {
  connect.disabled = true; state('connecting'); status('Getting ready to listen…');
  try { orb = new VoiceOrb(); } catch { /* Voice works even without visual analysis. */ }
  try {
    const response = await fetch('/api/ui/session', {method:'POST', headers});
    if (!response.ok) throw new Error(response.status === 403 ? 'Open this page at localhost:8000 to start.' : `Could not start the conversation (${response.status}).`);
    const data = await response.json(); sessionId = data.session_id; sessionToken = data.bridge_token;
    connection = new AudioConnection(sessionToken, text => {
      status(text.startsWith('Listening') ? 'Listening. Go ahead, I’m here.' : text);
    }, {onTrack:(kind, track) => orb?.watch(kind, track)});
    await connection.connect(); state('connected');
    connect.querySelector('span').textContent = 'Conversation active'; disconnect.disabled = false;
  } catch (error) {
    try { await stop(); } finally { state('error'); status(error.message); }
  }
};
disconnect.onclick = () => stop().catch(() => { state('error'); status('Could not finish cleanup. Check the launcher.'); });
window.addEventListener('pagehide', () => {
  const token = connection?.token || sessionToken;
  connection?.close(); orb?.close();
  if (sessionId && token) fetch('/api/ui/leave', {method:'POST', keepalive:true, headers,
    body:JSON.stringify({token})}).catch(() => {});
});
