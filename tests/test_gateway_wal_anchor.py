"""A business-process RO connection retains WAL metadata, never a snapshot."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import threading

import pytest

from test_gemini_gateway import KEY,SECRET,setup
from test_gateway_supersession import reply


def configured(tmp_path,monkeypatch,*,enabled=True,wal=True):
    app,gateway,path=setup(tmp_path,lambda r:reply())
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA journal_mode='+('WAL' if wal else 'DELETE')).fetchone()[0]==('wal' if wal else 'delete')
        db.execute('CREATE TABLE anchor_probe(value INTEGER)')
        db.execute('INSERT INTO anchor_probe VALUES (1)');db.commit()
    gateway.config_loader=lambda:{'GEMINI_GATEWAY_SECRET':SECRET,'GEMINI_API_KEY':KEY,
        **({'SERVICEVOICE_TIMING_PERSISTENCE':'on'} if enabled else {})}
    async def faq_ready():return True
    monkeypatch.setattr(gateway.faq,'start',faq_ready)
    return gateway,path


def sidecars(path):
    return [Path(str(path)+suffix) for suffix in ('-wal','-shm')]


def test_readonly_anchor_retains_sidecars_without_snapshot_or_checkpoint_lock(tmp_path,monkeypatch):
    gateway,path=configured(tmp_path,monkeypatch)
    assert not any(p.exists() for p in sidecars(path))
    async def run():
        try:
            assert await gateway.start()
            connection=gateway._wal_anchor
            assert gateway.wal_anchor_status=='ready' and connection is not None
            assert connection.isolation_level is None and connection.in_transaction is False
            assert connection.execute('PRAGMA query_only').fetchone()[0]==1
            assert connection.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
            assert all(p.exists() for p in sidecars(path))
            with pytest.raises(sqlite3.OperationalError,match='readonly'):
                connection.execute('CREATE TABLE forbidden(value)')
            # Prove URI mode=ro itself prevents writes, independently of the
            # additional query_only defense. Restore the production pragma.
            connection.execute('PRAGMA query_only=OFF').fetchall()
            with pytest.raises(sqlite3.OperationalError,match='readonly'):
                connection.execute('CREATE TABLE still_forbidden(value)')
            connection.execute('PRAGMA query_only=ON').fetchall()
            for expected in (2,3):
                with closing(sqlite3.connect(path)) as writer:
                    writer.execute('INSERT INTO anchor_probe VALUES (?)',(expected,));writer.commit()
                # A new read-only reviewer joins an existing SHM/WAL. This local
                # test does not simulate a Linux read-only volume mount.
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,isolation_level=None)) as reviewer:
                    assert reviewer.execute('SELECT MAX(value) FROM anchor_probe').fetchone()[0]==expected
                assert connection.execute('SELECT MAX(value) FROM anchor_probe').fetchone()[0]==expected
                assert connection.in_transaction is False
                with closing(sqlite3.connect(path)) as writer:
                    assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()==(0,0,0)
                assert all(p.exists() for p in sidecars(path))
            await gateway.close();await gateway.close()
            assert gateway._wal_anchor is None and gateway.wal_anchor_status=='closed'
            with pytest.raises(sqlite3.ProgrammingError,match='closed'):connection.execute('SELECT 1')
            # A final RO close need not unlink sidecars. Normal SQLite writer
            # cleanup must succeed; never manually remove metadata files.
            with closing(sqlite3.connect(path)) as writer:
                assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()==(0,0,0)
            assert not any(p.exists() for p in sidecars(path))
        finally:await gateway.close()
    asyncio.run(run())


def test_concurrent_and_repeated_start_create_exactly_one_anchor(tmp_path,monkeypatch):
    gateway,path=configured(tmp_path,monkeypatch)
    original=gateway._open_wal_anchor;connections=[]
    def opened():
        connection=original();connections.append(connection);return connection
    monkeypatch.setattr(gateway,'_open_wal_anchor',opened)
    async def run():
        try:
            assert all(await asyncio.gather(*(gateway.start() for _ in range(8))))
            assert await gateway.start()
            assert len(connections)==1 and gateway._wal_anchor is connections[0]
        finally:await gateway.close()
        with pytest.raises(sqlite3.ProgrammingError,match='closed'):connections[0].execute('SELECT 1')
    asyncio.run(run())


def test_default_off_does_not_open_observation_connection(tmp_path,monkeypatch):
    gateway,_=configured(tmp_path,monkeypatch,enabled=False)
    def prohibited():raise AssertionError('disabled anchor opened')
    monkeypatch.setattr(gateway,'_open_wal_anchor',prohibited)
    async def run():
        try:
            assert await gateway.start()
            assert gateway._wal_anchor_startup_task is None and gateway._wal_anchor is None
            assert gateway.wal_anchor_status=='disabled'
        finally:await gateway.close()
    asyncio.run(run())


def test_nonwal_database_is_not_reconfigured(tmp_path,monkeypatch):
    gateway,path=configured(tmp_path,monkeypatch,wal=False)
    async def run():
        try:
            assert await gateway.start()
            assert gateway.wal_anchor_status=='not_wal' and gateway._wal_anchor is None
            with closing(sqlite3.connect(path)) as db:
                assert db.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
            assert not any(p.exists() for p in sidecars(path))
        finally:await gateway.close()
    asyncio.run(run())


def test_enabled_anchor_failure_has_fixed_error_and_never_creates_missing_database(tmp_path,monkeypatch):
    gateway,_=configured(tmp_path,monkeypatch)
    missing=tmp_path/'PRIVATE_PATH_must_not_be_created.sqlite3';gateway.store.path=str(missing)
    async def run():
        try:
            with pytest.raises(RuntimeError) as error:await gateway.start()
            assert str(error.value)=='gateway_wal_anchor_unavailable'
            assert gateway.wal_anchor_status=='unavailable' and not missing.exists()
            with pytest.raises(RuntimeError,match='^gateway_wal_anchor_unavailable$'):await gateway.start()
        finally:await gateway.close()
    asyncio.run(run())


def test_cancelled_start_waiter_does_not_leak_late_connection_during_shutdown(tmp_path,monkeypatch):
    gateway,_=configured(tmp_path,monkeypatch)
    original=gateway._open_wal_anchor;opened=threading.Event();release=threading.Event();connections=[]
    def delayed():
        connection=original();connections.append(connection);opened.set()
        assert release.wait(3)
        return connection
    monkeypatch.setattr(gateway,'_open_wal_anchor',delayed)
    async def run():
        starter=asyncio.create_task(gateway.start());closer=None
        try:
            assert await asyncio.to_thread(opened.wait,2)
            starter.cancel();await asyncio.gather(starter,return_exceptions=True)
            closer=asyncio.create_task(gateway.close())
            await asyncio.sleep(0);assert gateway.closing
            release.set();await asyncio.wait_for(closer,2)
            assert gateway._wal_anchor is None and gateway.wal_anchor_status=='closed'
            assert await gateway.start() is False
            assert len(connections)==1
            with pytest.raises(sqlite3.ProgrammingError,match='closed'):connections[0].execute('SELECT 1')
        finally:
            release.set();await asyncio.gather(starter,return_exceptions=True)
            if closer:await closer
            await gateway.close()
    asyncio.run(run())
