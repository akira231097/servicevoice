"""One process/store/lifespan. Provider only by default; explicit local sandbox."""
from __future__ import annotations
import argparse
import asyncio
import fcntl
import ipaddress
import os
from pathlib import Path
import socket
import sqlite3
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SECRET_KEYS = ('VAPI_WEBHOOK_SECRET', 'GEMINI_GATEWAY_SECRET', 'GEMINI_API_KEY',
               'VAPI_PHONE_WEBHOOK_SECRET', 'TYPESAFE_API_KEY', 'LANGSMITH_API_KEY')


def load_secrets(values=os.environ, root=Path('/run/secrets')):
    for key in SECRET_KEYS:
        source = values.get(key + '_FILE')
        if not source:
            continue
        if values.get(key):
            raise RuntimeError('Configure each secret through only one mechanism.')
        filename = Path(source).resolve()
        if not filename.is_relative_to(root.resolve()):
            raise RuntimeError('Secret files must be mounted under /run/secrets.')
        with filename.open('rb') as handle:
            raw = handle.read(4097)
        if not 1 <= len(raw) <= 4096:
            raise RuntimeError('Secret file size is invalid.')
        value = raw.decode('utf-8').rstrip('\r\n')
        if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise RuntimeError('Secret file content is invalid.')
        values[key] = value


def lock_data(path, root=Path('/data')):
    path = Path(path).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise RuntimeError('Database must be inside the explicit /data volume.')
    if not path.parent.is_dir():
        raise RuntimeError('Database parent directory must already exist.')
    handle = (root / '.servicevoice.lock').open('a+b')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('A ServiceVoice process already owns this data volume.') from None
    return handle


class LocalBoundary:
    """Only the dedicated bridge gateway/loopback and local Host may use UI.

    Docker's host port MUST be published on127.0.0.1. This adapter does not
    substitute for that host firewall boundary. Forwarded headers are ignored.
    """
    def __init__(self, app, gateway):
        address = ipaddress.ip_address(gateway)
        if not address.is_private or address.is_unspecified or address.is_multicast:
            raise ValueError('Local gateway must be an explicit private IP.')
        self.app, self.gateway = app, str(address)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.reject(send, scope['type'])
        peer = (scope.get('client') or ('', 0))[0]
        headers = scope.get('headers', [])
        hosts = [value.decode('latin1') for key, value in headers if key.lower() == b'host']
        valid = len(hosts) == 1 and peer in (self.gateway, '127.0.0.1', '::1')
        host = hosts[0] if len(hosts) == 1 else ''
        try:
            parsed = urlsplit('http://' + host)
            valid = valid and parsed.hostname in ('localhost', '127.0.0.1', '::1') and parsed.username is None and parsed.password is None
            valid = valid and not parsed.path and not parsed.query and not parsed.fragment and parsed.port is not None
        except ValueError:
            valid = False
        for key, value in headers:
            if key.lower() == b'origin' and value.decode('latin1').rstrip('/') != 'http://' + host:
                valid = False
            if key.lower() == b'sec-fetch-site' and value not in (b'', b'none', b'same-origin'):
                valid = False
        if not valid:
            return await self.reject(send)
        forwarded = dict(scope)
        forwarded['client'] = ('127.0.0.1', (scope.get('client') or ('', 0))[1])
        forwarded['headers'] = [(key, value) for key, value in headers
                                if key.lower() not in (b'forwarded', b'x-forwarded-for', b'x-forwarded-host', b'x-forwarded-proto')]
        await self.app(forwarded, receive, send)

    @staticmethod
    async def reject(send, kind='http'):
        if kind == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        body = b'{"detail":"Local deployment access denied."}'
        await send({'type': 'http.response.start', 'status': 403,
                    'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode()), (b'cache-control', b'no-store')]})
        await send({'type': 'http.response.body', 'body': body})


class SocketDispatcher:
    def __init__(self, public, local=None):
        self.public, self.local = public, local

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self.public(scope, receive, send)
        port = (scope.get('server') or ('', 0))[1]
        if port == 8000:
            return await self.public(scope, receive, send)
        if port == 8001 and self.local is not None:
            return await self.local(scope, receive, send)
        return await LocalBoundary.reject(send, scope['type'])


def ready(mode, console, values=os.environ):
    try:
        with sqlite3.connect(Path(console.state.store.path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.2) as db:
            db.execute('SELECT id FROM sessions LIMIT 0').fetchall()
        if mode == 'local':
            return True
        return bool(console.state.telephony_ingress.config is not None
                    and values.get('VAPI_MODEL_PROFILE') == 'gemini-3.8-flash-custom'
                    and values.get('GEMINI_API_KEY') and len(values.get('GEMINI_GATEWAY_SECRET', '')) >= 32)
    except (sqlite3.Error, OSError):
        return False


def listener(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', port)); sock.listen(128); sock.setblocking(False)
    return sock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('provider', 'local'), default='provider')
    options = parser.parse_args()
    from runtime_sqlite import ensure_sqlite
    ensure_sqlite()  # Must run before importing application code.
    load_secrets()
    path = os.environ.get('SERVICEVOICE_DB', '/data/servicevoice.sqlite3')
    lock = lock_data(path)
    os.environ['SERVICEVOICE_DB'] = str(Path(path).resolve())
    sockets = []
    try:
        from app.main import app as console, public_app
        from fastapi.responses import JSONResponse
        import uvicorn
        @public_app.get('/healthz')
        def health():
            return JSONResponse({'status': 'ok'}, headers={'Cache-Control': 'no-store'})
        @public_app.get('/readyz')
        def readiness():
            available = ready(options.mode, console)
            return JSONResponse({'status': 'ready' if available else 'unavailable'}, status_code=200 if available else 503,
                                headers={'Cache-Control': 'no-store'})
        local = None
        sockets.append(listener(8000))
        if options.mode == 'local':
            gateway = os.environ.get('SERVICEVOICE_LOCAL_PEER')
            if not gateway:
                raise RuntimeError('Local mode requires its exact dedicated bridge gateway.')
            local = LocalBoundary(console, gateway)
            sockets.append(listener(8001))
        deployment = SocketDispatcher(public_app, local)
        server = uvicorn.Server(uvicorn.Config(deployment, host='0.0.0.0', port=8000, proxy_headers=False,
            workers=1, access_log=False, timeout_keep_alive=5, timeout_graceful_shutdown=20,
            limit_concurrency=64, backlog=128, h11_max_incomplete_event_size=16384))
        asyncio.run(server.serve(sockets=sockets))
    finally:
        for sock in sockets:
            sock.close()
        lock.close()


if __name__ == '__main__':
    main()
