"""Prepare verified TLS once before entering the real-time voice event loop."""
import os
import ssl
import threading

import certifi
from google.genai import types
from livekit.agents import APIConnectOptions

_tls_lock = threading.Lock()
_tls_context = None


def tls_context() -> ssl.SSLContext:
    global _tls_context
    with _tls_lock:
        if _tls_context is None:
            _tls_context = ssl.create_default_context(cafile=os.environ.get('SSL_CERT_FILE', certifi.where()),
                                                      capath=os.environ.get('SSL_CERT_DIR'))
        return _tls_context


def warm_network_runtime() -> None:
    # httpcore builds its module-level contexts on first import too.
    import httpcore  # noqa: F401
    tls_context()


def google_http_options() -> types.HttpOptions:
    context = tls_context()
    return types.HttpOptions(timeout=int(APIConnectOptions().timeout * 1000),
        client_args={'verify': context}, async_client_args={'verify': context, 'ssl': context,
            # Keep sending keepalives, but don't locally kill an idle Live socket
            # merely because its peer delays pongs. Real close/send/receive errors
            # still go through SDK recovery; this is not an audio/VAD timeout.
            'ping_interval': 10, 'ping_timeout': None, 'close_timeout': 3})
