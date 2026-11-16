"""Double-click launcher; bootstrap with the standard library before SDK imports."""
import argparse
import hashlib
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ('ADMIN_API_KEY', 'BRIDGE_SIGNING_SECRET', 'LIVEKIT_URL',
            'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET', 'GOOGLE_API_KEY')


def local_configuration() -> dict[str, str]:
    # Read values as data; never execute a .env file as a shell script.
    values = {}
    env_file = ROOT / '.env'
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                name, value = line.split('=', 1)
                values[name.strip()] = value.strip().strip('\"\'')
    values.update(os.environ)
    return values


def missing_credentials(values: dict[str, str]) -> list[str]:
    return [name for name in REQUIRED if not values.get(name) or
            values[name].startswith(('your-', 'replace-')) or
            (name in {'ADMIN_API_KEY', 'BRIDGE_SIGNING_SECRET'} and len(values[name]) < 32) or
            'your-project' in values[name]]


def stop(children: list[subprocess.Popen]) -> None:
    for child in children:
        if child.poll() is None:
            try:
                child.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
            except OSError:
                pass
    for child in children:
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def supervise(children, commands, flags):
    """Recover a failed service without terminating its healthy peer."""
    roles = ('API', 'voice worker')
    failures = [0] * len(children)
    retry_at = [0.] * len(children)
    started_at = [time.monotonic()] * len(children)
    while True:
        for index, child in enumerate(children):
            code = child.poll()
            if code is None:
                if time.monotonic() - started_at[index] >= 60:
                    failures[index] = 0
                continue
            if not retry_at[index]:
                failures[index] += 1
                delay = min(30, 2 ** min(failures[index] - 1, 5))
                print(f'{roles[index]} exited (PID {child.pid}, code {code}); '
                      f'restarting in {delay}s. Other service retained.', flush=True)
                retry_at[index] = time.monotonic() + delay
            if time.monotonic() < retry_at[index]:
                continue
            try:
                replacement = subprocess.Popen(commands[index], creationflags=flags)
            except OSError as exc:
                print(f'{roles[index]} restart failed ({type(exc).__name__}); retrying in 30s.', flush=True)
                retry_at[index] = time.monotonic() + 30
                continue
            children[index] = replacement
            started_at[index] = time.monotonic()
            retry_at[index] = 0.
            print(f'{roles[index]} restarted (PID {replacement.pid}).', flush=True)
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description='Set up and run the meeting voice agent')
    parser.add_argument('--check', action='store_true', help='Check configuration without installing or starting')
    parser.add_argument('--no-browser', action='store_true', help='Start services for the meeting launcher')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        print('Python 3.11 or newer is required. Python 3.13 is recommended.')
        return 1
    os.chdir(ROOT)
    env_file = ROOT / '.env'
    if not env_file.exists() and not args.check:
        template = (ROOT / '.env.example').read_text(encoding='utf-8')
        template = template.replace('replace-with-a-random-secret-at-least-32-characters', secrets.token_urlsafe(48))
        template = template.replace('replace-with-another-random-secret-at-least-32-characters', secrets.token_urlsafe(48))
        # Exclusive creation protects an existing configuration from accidental overwrite.
        try:
            with env_file.open('x', encoding='utf-8') as file:
                file.write(template)
        except FileExistsError:
            pass
        else:
            print('Created .env with independent random application secrets.')
    values = local_configuration()
    missing = missing_credentials(values)
    if missing:
        print('Fill in these settings in .env: ' + ', '.join(missing))
        print('Then double-click Run-Agent.cmd again. Recall and Tavily keys are optional for local voice.')
        if not args.check and os.name == 'nt':
            subprocess.Popen(['notepad.exe', str(env_file)])
        return 2
    if args.check:
        print('Python and local voice credential fields are configured (provider access is not tested).')
        return 0

    environment = ROOT / '.venv'
    executable = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not executable.exists():
        print('Creating Python environment...', flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    fingerprint = hashlib.sha256((ROOT / 'requirements.lock').read_bytes() +
                                (ROOT / 'pyproject.toml').read_bytes()).hexdigest()
    marker = environment / '.launcher-ready'
    if not marker.exists() or marker.read_text().strip() != fingerprint:
        print('Installing dependencies. This first run needs internet access...', flush=True)
        subprocess.run([str(executable), '-m', 'pip', 'install', '-r', 'requirements.lock'], check=True)
        subprocess.run([str(executable), '-m', 'pip', 'install', '--no-deps', '-e', '.'], check=True)
        subprocess.run([str(executable), '-m', 'pip', 'check'], check=True)
        marker.write_text(fingerprint)

    try:
        api_port = urlsplit(values.get('API_BASE_URL', 'http://localhost:8000')).port or 80
    except ValueError:
        print('Set API_BASE_URL to a valid local API URL in .env.')
        return 2
    api_origin = f'http://127.0.0.1:{api_port}'
    # Check the exact listening address before launching to avoid opening an unrelated server.
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', api_port))
        except OSError:
            print(f'Port {api_port} is already in use. Set API_BASE_URL to an unused local port or close the existing server.')
            return 1
    print('Checking the Gemini Live connection...', flush=True)
    result = subprocess.run([str(executable), '-m', 'app.agent.diagnostics'])
    if result.returncode:
        return result.returncode
    children = []
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    commands = [
        [str(executable), '-m', 'uvicorn', 'app.main:create_app',
         '--factory', '--host', '127.0.0.1', '--port', str(api_port), '--no-access-log'],
        [str(executable), '-m', 'app.agent.worker', 'start'],
    ]
    try:
        print('Starting API and worker. Press Ctrl+C here to stop both.', flush=True)
        for command in commands:
            children.append(subprocess.Popen(command, creationflags=flags))
        deadline = time.monotonic() + 90
        # A direct loopback request must not be routed through an environment HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                for role, child in zip(('API', 'voice worker'), children):
                    if child.poll() is not None:
                        print(f'{role} exited during startup (PID {child.pid}, code {child.poll()}).', flush=True)
                return 1
            try:
                with opener.open(f'{api_origin}/health', timeout=1) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(.5)
        else:
            print('API startup timed out. Check the messages above.')
            return 1
        if not args.no_browser:
            webbrowser.open(api_origin)
            print('Click Start conversation on the voice page and allow microphone access.', flush=True)
        print('For meetings, configure Recall and public HTTPS as described in README.md.', flush=True)
        supervise(children, commands, flags)
    except KeyboardInterrupt:
        print('\nStopping services...')
        return 0
    finally:
        stop(children)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f'Startup failed ({type(exc).__name__}). Check the preceding message and retry.')
        raise SystemExit(1) from None
