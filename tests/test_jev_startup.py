"""Cold initialization must never occupy a caller's350ms routing budget."""
import asyncio
import threading
import time

import dotenv
import httpx
import pytest

from app.jev_faq import JevFaqRouter


class Client:
    def __init__(self):self.is_closed = False
    async def aclose(self):self.is_closed = True


def test_slow_factory_runs_offloop_once_and_live_path_never_waits(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    factory_threads = []
    router = JevFaqRouter(None)
    created = Client()
    def build():
        factory_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return created
    monkeypatch.setattr(router, "_build_client", build)
    async def scenario():
        loop_thread = threading.get_ident()
        first = asyncio.create_task(router.start())
        second = asyncio.create_task(router.start())
        try:
            while not entered.is_set():await asyncio.sleep(.001)
            # The blocking factory remains held while the event loop and an
            # eligible live request continue; no constructor or DB read occurs.
            result = await asyncio.wait_for(router.choose("sid", "cid", [{"role": "user", "content": "What are your hours?"}], "synthetic-key"), .1)
            assert result.reason == "classifier_not_ready" and not router.ready
            first.cancel()
            with pytest.raises(asyncio.CancelledError):await first
            assert not second.done()
            release.set()
            assert await second is True and router.ready
            assert factory_threads == [factory_threads[0]] and factory_threads[0] != loop_thread
            assert await router.start() is True and len(factory_threads) == 1
            assert router.initialization_ms is not None
        finally:
            release.set()
            await router.close()
        assert created.is_closed
    asyncio.run(scenario())


def test_close_is_bounded_and_late_created_client_cannot_publish(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    router, created = JevFaqRouter(None), Client()
    def build():
        entered.set()
        assert release.wait(3)
        return created
    monkeypatch.setattr(router, "_build_client", build)
    async def scenario():
        startup = asyncio.create_task(router.start())
        try:
            while not entered.is_set():await asyncio.sleep(.001)
            began = time.monotonic()
            await router.close()
            assert time.monotonic()-began < 1.5
            assert not startup.done() and not router.ready
            release.set()
            assert await startup is False
            assert created.is_closed and router.client is None
            assert await router.start() is False
        finally:
            release.set()
            await router.close()
    asyncio.run(scenario())


def test_failed_startup_stays_optional_and_never_retries_in_request(monkeypatch):
    router, builds = JevFaqRouter(None), []
    def build():
        builds.append(True)
        raise RuntimeError("PRIVATE environment diagnostic")
    monkeypatch.setattr(router, "_build_client", build)
    async def scenario():
        try:
            assert await router.start() is False
            assert await router.start() is False
            result = await router.choose("sid", "cid", [{"role": "user", "content": "What are your hours?"}], "synthetic-key")
            assert result.reason == "classifier_not_ready" and result.requests == 0
            assert len(builds) == 1 and "PRIVATE" not in repr(result)
        finally:await router.close()
    asyncio.run(scenario())


def test_injected_client_is_preserved_and_not_owned(monkeypatch):
    created = Client()
    router = JevFaqRouter(None, client=created)
    monkeypatch.setattr(router, "_build_client", lambda: pytest.fail("Injected client must be preserved"))
    async def scenario():
        assert await router.start() is True
        assert router.connection() is created
        await router.close()
        assert not created.is_closed and not router.ready
    asyncio.run(scenario())


def test_both_listener_lifespans_share_startup_and_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *args, **kwargs: {})
    monkeypatch.setenv("SERVICEVOICE_DB", str(tmp_path / "import.sqlite3"))
    from app.main import create_app, create_public_app
    console = create_app(tmp_path / "two-listeners.sqlite3")
    public = create_public_app(console)
    created, builds = Client(), []
    def build():
        builds.append(True)
        return created
    monkeypatch.setattr(console.state.gemini_gateway.faq, "_build_client", build)
    async def scenario():
        async with console.router.lifespan_context(console):
            async with public.router.lifespan_context(public):
                assert console.state.gemini_gateway.faq.ready
                assert len(builds) == 1
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=console), base_url="http://testserver") as http:
                    config = (await http.get("/api/config")).json()
                    assert config["jev_faq_ready"] is True
                    assert config["jev_pool_initialization_ms"] is not None
                    assert config["jev_faq_version"] == "jev-static-faq-2026-10-21-1"
        assert created.is_closed
    asyncio.run(scenario())
