"""Join a consenting meeting from one URL, using local .env credentials."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from urllib.parse import urlparse

import httpx

from app.config.settings import ROOT, Settings
from app.meeting.security import validate_meeting_url
from app.observability.conversation import ConversationConsole


def healthy(client: httpx.Client, origin: str) -> bool:
    try:
        response = client.get(origin.rstrip('/') + '/health', timeout=3)
        return response.status_code == 200 and response.json().get('status') in {'ok', 'degraded'}
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def wait_ready(client: httpx.Client, origin: str, process: subprocess.Popen, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if healthy(client, origin):
            return
        if process.poll() is not None:
            raise RuntimeError('Startup process exited; inspect data/meeting-*.log')
        time.sleep(1)
    raise RuntimeError('Startup timed out; inspect data/meeting-*.log')


def stop_owned(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
        process.wait(timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    settings = Settings()
    print('Only use meetings whose participants consent to the assistant and transcription.')
    meeting_url = input('Authorized, consenting Google Meet / Zoom / Teams URL: ').strip()
    try:
        validate_meeting_url(meeting_url)
    except ValueError as exc:
        print(str(exc))
        return 1
    public = urlparse(settings.public_base_url)
    if public.scheme != 'https' or not public.hostname or public.path not in {'', '/'}:
        print('Set PUBLIC_BASE_URL to your ngrok HTTPS origin in .env first.')
        return 1
    executable = shutil.which('ngrok')
    if not executable or not settings.ngrok_authtoken.get_secret_value():
        print('Install ngrok and set NGROK_AUTHTOKEN in .env first.')
        return 1
    if not settings.recall_api_key.get_secret_value() or not settings.recall_webhook_secret.get_secret_value():
        print('Set Recall API and workspace signing keys in .env first.')
        return 1
    environment = os.environ.copy()
    environment['NGROK_AUTHTOKEN'] = settings.ngrok_authtoken.get_secret_value()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    owned = []
    session_id = None
    cleanup = False
    exit_code = 0
    with ExitStack() as stack:
        client = stack.enter_context(httpx.Client(timeout=45, follow_redirects=False))
        auth = {'Authorization': 'Bearer ' + settings.admin_api_key.get_secret_value()}
        try:
            (ROOT / 'data').mkdir(exist_ok=True)
            if not healthy(client, settings.api_base_url):
                print('Starting API and voice worker...', flush=True)
                log = stack.enter_context((ROOT / 'data/meeting-services.log').open('w', encoding='utf-8'))
                process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/launch.py'), '--no-browser'],
                    cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
                owned.append(process)
                wait_ready(client, settings.api_base_url, process, 180)
            runtime_health = client.get(settings.api_base_url.rstrip('/') + '/health').json()
            if runtime_health.get('protocol_version', 0) < (12 if settings.screen_enabled else 10):
                raise RuntimeError('An older API is still running. Close the old Run-Agent.cmd window, then restart Join-Meeting.cmd.')
            if not healthy(client, settings.public_base_url):
                print('Starting public audio tunnel...', flush=True)
                log = stack.enter_context((ROOT / 'data/meeting-tunnel.log').open('w', encoding='utf-8'))
                process = subprocess.Popen([executable, 'http', 'http://127.0.0.1:8000', '--url', settings.public_base_url,
                    '--log', 'stdout', '--log-format', 'json', '--log-level', 'warn'],
                    cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
                owned.append(process)
                wait_ready(client, settings.public_base_url, process, 45)
            runtime_screen = runtime_health.get('screen_features')
            if runtime_screen is not None:
                screen_message = ('available on request; voice input runs independently' if runtime_screen.get('capture') and runtime_screen.get('vision') else
                                  'capture enabled, vision disabled' if runtime_screen.get('capture') else
                                  'disabled; the agent will not receive shared-screen images')
                print('[Screen] ' + screen_message, flush=True)
            else:
                print('[Screen] Runtime capability status unavailable; restart services to load screen diagnostics.', flush=True)
            print('Sending the assistant to the meeting. Admit it if the platform asks...', flush=True)
            response = client.post(settings.api_base_url.rstrip('/') + '/api/meetings', headers=auth,
                json={'meeting_url': meeting_url, 'consent_confirmed': True})
            response.raise_for_status()
            result = response.json()
            session_id = result['session_id']
            (ROOT / 'data/last-meeting.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            print('Meeting session: ' + session_id, flush=True)
            print('Keep this window open. Press Ctrl+C to leave.', flush=True)
            last_status = None
            console = ConversationConsole(lambda text: print(text, flush=True))
            conversation_api_available = True
            failures = 0
            while True:
                try:
                    response = client.get(settings.api_base_url.rstrip('/') + f'/api/meetings/{session_id}', headers=auth)
                    response.raise_for_status()
                    status = response.json()['status']
                    if status in {'done', 'left', 'failed'}:
                        print('Meeting status: ' + status, flush=True)
                        cleanup = True
                        break
                    feed = client.get(settings.api_base_url.rstrip('/') + f'/api/meetings/{session_id}/live-context',
                                      headers=auth, params={'after': console.cursor})
                    feed.raise_for_status()
                    payload = feed.json()
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    failures += 1
                    if failures == 1:
                        print(f'Meeting monitoring interrupted ({type(exc).__name__}); retrying. '
                              'The meeting has not been left. Press Ctrl+C to leave.', flush=True)
                    time.sleep(min(10, .5 * 2 ** min(failures, 5)))
                    continue
                if failures:
                    print('Meeting monitoring reconnected.', flush=True)
                    failures = 0
                if status != last_status:
                    print('Meeting status: ' + status, flush=True)
                    last_status = status
                if 'messages' in payload:
                    try:
                        console.display(payload)
                    except (KeyError, TypeError, ValueError):
                        if conversation_api_available:
                            print('Conversation display received incomplete data; meeting retained.', flush=True)
                            conversation_api_available = False
                elif conversation_api_available:
                    print('Live conversation display unavailable. Restart the API to load the updated endpoints.', flush=True)
                    conversation_api_available = False
                time.sleep(.5)
        except KeyboardInterrupt:
            cleanup = True
            print('\nLeaving the meeting...', flush=True)
        except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
            code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
            print(f'Meeting startup or monitoring failed ({code}). Check data/meeting-*.log and the server logs.')
            if isinstance(exc, RuntimeError):
                print(str(exc))
            if session_id is None:
                print('For an ambiguous join timeout, inspect Recall before retrying to avoid duplicate bots.')
            exit_code = 1
        finally:
            if session_id and cleanup:
                try:
                    response = client.post(settings.api_base_url.rstrip('/') + f'/api/meetings/{session_id}/leave',
                                           headers=auth)
                    response.raise_for_status()
                except httpx.HTTPError:
                    print('Leave could not be confirmed. Check Recall or the leave CLI for session ' + session_id)
                    exit_code = 1
            if cleanup or session_id is None:
                for process in reversed(owned):
                    stop_owned(process)
            else:
                print('Services retained for the active meeting. Use the leave CLI or Recall to leave session ' + session_id)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
