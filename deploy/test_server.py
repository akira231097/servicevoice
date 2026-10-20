"""Run inside the Linux application image; no network or provider calls."""
import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import LocalBoundary, SocketDispatcher, load_secrets, lock_data, ready


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, *, peer='172.30.88.1', host=b'127.0.0.1:8875', extras=(), port=8001, local=True, kind='http'):
        events, seen = [], []
        async def target(scope, receive, send):
            seen.append(scope)
            await send({'type': 'http.response.start', 'status': 204, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})
        boundary = LocalBoundary(target, '172.30.88.1') if local else None
        dispatcher = SocketDispatcher(target, boundary)
        scope = {'type': kind, 'server': ('0.0.0.0', port), 'client': (peer, 10000),
                 'headers': [(b'host', host), *extras]}
        async def receive(): return {'type': 'http.request', 'body': b''}
        async def send(event): events.append(event)
        await dispatcher(scope, receive, send)
        return events, seen

    async def test_exact_peer_and_local_host_permit_operator_loopback_mapping(self):
        events, seen = await self.request(extras=[(b'origin', b'http://127.0.0.1:8875')])
        self.assertEqual(events[0]['status'], 204)
        self.assertEqual(seen[0]['client'][0], '127.0.0.1')

    async def test_foreign_peer_cannot_spoof_forwarded_loopback(self):
        events, seen = await self.request(peer='172.30.88.9', extras=[(b'x-forwarded-for', b'127.0.0.1')])
        self.assertEqual(events[0]['status'], 403); self.assertFalse(seen)

    async def test_foreign_host_duplicate_host_and_url_userinfo_are_rejected(self):
        for host, extras in [(b'evil.example:8875', []), (b'127.0.0.1:8875', [(b'host', b'localhost:8875')]),
                             (b'evil@127.0.0.1:8875', []), (b'127.0.0.1:8875/path', [])]:
            events, seen = await self.request(host=host, extras=extras)
            self.assertEqual(events[0]['status'], 403); self.assertFalse(seen)

    async def test_cross_origin_and_fetch_metadata_rejected(self):
        for extra in [(b'origin', b'http://evil.example'), (b'sec-fetch-site', b'cross-site')]:
            events, seen = await self.request(extras=[extra])
            self.assertEqual(events[0]['status'], 403); self.assertFalse(seen)

    async def test_forwarded_headers_removed_for_valid_local_request(self):
        _, seen = await self.request(extras=[(b'x-forwarded-for', b'198.51.100.1'), (b'forwarded', b'host=evil')])
        self.assertEqual(seen[0]['headers'], [(b'host', b'127.0.0.1:8875')])

    async def test_provider_mode_has_no_console_listener_dispatch(self):
        events, seen = await self.request(local=False)
        self.assertEqual(events[0]['status'], 403); self.assertFalse(seen)

    async def test_provider_socket_never_uses_local_mapping(self):
        events, seen = await self.request(peer='198.51.100.1', host=b'provider.example', port=8000)
        self.assertEqual(events[0]['status'], 204)
        self.assertEqual(seen[0]['client'][0], '198.51.100.1')

    async def test_lifespan_has_only_one_public_target(self):
        _, seen = await self.request(kind='lifespan')
        self.assertEqual(len(seen), 1); self.assertEqual(seen[0]['type'], 'lifespan')

    async def test_local_websocket_rejects_with_websocket_close_not_http(self):
        events, seen = await self.request(kind='websocket')
        self.assertEqual(events, [{'type': 'websocket.close', 'code': 1008}]); self.assertFalse(seen)

    async def test_unknown_socket_websocket_uses_same_protocol_safe_rejection(self):
        events, seen = await self.request(kind='websocket', port=9000)
        self.assertEqual(events, [{'type': 'websocket.close', 'code': 1008}]); self.assertFalse(seen)


class ConfigurationTests(unittest.TestCase):
    def test_single_instance_lock_rejects_second_owner_then_releases(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); first = lock_data(root / 'state.sqlite3', root)
            try:
                with self.assertRaisesRegex(RuntimeError, 'already owns'):
                    lock_data(root / 'state.sqlite3', root)
            finally: first.close()
            lock_data(root / 'state.sqlite3', root).close()

    def test_database_cannot_escape_volume(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self.assertRaisesRegex(RuntimeError, 'inside'):
                lock_data(root.parent / 'outside.sqlite3', root)

    def test_mounted_secret_loads_without_changing_file(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); file = root / 'key'; file.write_text('synthetic-test-key\n')
            values = {'GEMINI_API_KEY_FILE': str(file)}
            load_secrets(values, root)
            self.assertEqual(values['GEMINI_API_KEY'], 'synthetic-test-key')
            self.assertEqual(file.read_text(), 'synthetic-test-key\n')

    def test_custom_speech_secret_uses_same_private_file_boundary(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); file = root / 'tts'; file.write_text('synthetic-speech-' + 's' * 32 + '\n')
            values = {'GEMINI_TTS_BEARER_SECRET_FILE': str(file)}
            load_secrets(values, root)
            self.assertEqual(values['GEMINI_TTS_BEARER_SECRET'], 'synthetic-speech-' + 's' * 32)
            with self.assertRaisesRegex(RuntimeError, 'one mechanism'):
                load_secrets(values, root)
            with self.assertRaisesRegex(RuntimeError, 'mounted under'):
                load_secrets({'GEMINI_TTS_BEARER_SECRET_FILE': str(root.parent / 'outside')}, root)

    def test_ambiguous_outside_and_oversized_secrets_fail(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); file = root / 'key'; file.write_text('x' * 4097)
            for values in [{'GEMINI_API_KEY_FILE': str(file), 'GEMINI_API_KEY': 'existing'},
                           {'GEMINI_API_KEY_FILE': str(root.parent / 'outside')},
                           {'GEMINI_API_KEY_FILE': str(file)}]:
                with self.assertRaises(RuntimeError): load_secrets(values, root)

    def test_readiness_distinguishes_keyless_local_and_unconfigured_provider(self):
        with tempfile.TemporaryDirectory() as name:
            file = Path(name) / 'state.sqlite3'
            with sqlite3.connect(file) as db: db.execute('CREATE TABLE sessions(id TEXT)')
            console = types.SimpleNamespace(state=types.SimpleNamespace(store=types.SimpleNamespace(path=file),
                telephony_ingress=types.SimpleNamespace(config=None)))
            self.assertTrue(ready('local', console, {})); self.assertFalse(ready('provider', console, {}))
            console.state.telephony_ingress.config = object()
            self.assertTrue(ready('provider', console, {'GEMINI_API_KEY': 'synthetic', 'GEMINI_GATEWAY_SECRET': 's' * 32,
                                                      'VAPI_MODEL_PROFILE': 'gemini-3.8-flash-custom'}))

    def test_readiness_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as name:
            file = Path(name) / 'missing.sqlite3'
            console = types.SimpleNamespace(state=types.SimpleNamespace(store=types.SimpleNamespace(path=file)))
            self.assertFalse(ready('local', console, {})); self.assertFalse(file.exists())

    def test_gemini_speech_readiness_requires_matching_model_voice_and_separate_secret(self):
        with tempfile.TemporaryDirectory() as name:
            file = Path(name) / 'state.sqlite3'
            with sqlite3.connect(file) as db: db.execute('CREATE TABLE sessions(id TEXT)')
            console = types.SimpleNamespace(state=types.SimpleNamespace(store=types.SimpleNamespace(path=file),
                telephony_ingress=types.SimpleNamespace(config=object())))
            values = {'GEMINI_API_KEY': 'synthetic', 'GEMINI_GATEWAY_SECRET': 'g' * 32,
                      'VAPI_PHONE_WEBHOOK_SECRET': 'p' * 32, 'VAPI_MODEL_PROFILE': 'gemini-3.8-flash-custom',
                      'VAPI_VOICE_PROFILE': 'gemini-3.8-flash-lite-tts', 'GEMINI_TTS_BEARER_SECRET': 's' * 32}
            self.assertTrue(ready('provider', console, values))
            for change in ({'GEMINI_TTS_BEARER_SECRET': ''}, {'GEMINI_TTS_BEARER_SECRET': 'g' * 32},
                           {'GEMINI_TTS_BEARER_SECRET': 'p' * 32}, {'GEMINI_TTS_MODEL': 'gemini-3.8-flash-tts'},
                           {'GEMINI_TTS_VOICE': 'invented'}):
                with self.subTest(change=tuple(change)):
                    self.assertFalse(ready('provider', console, {**values, **change}))


if __name__ == '__main__': unittest.main()
