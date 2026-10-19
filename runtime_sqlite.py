"""Use a verified project-private SQLite build on Windows; never alter Python."""
from pathlib import Path
import hashlib
import os
import sys

ROOT = Path(__file__).resolve().parent
DLL_SHA256 = "4b59d3cded9dc1608544160f3b4b10ac974c6ac0d6ffc23064e5f4c8dbd05aea"
_library = None


def ensure_sqlite():
    global _library
    if os.name == "nt" and "_sqlite3" not in sys.modules:
        dll = ROOT / ".local/sqlite/sqlite3.dll"
        if dll.exists():
            if hashlib.sha256(dll.read_bytes()).hexdigest() != DLL_SHA256:
                raise RuntimeError("Project SQLite library failed its pinned integrity check.")
            import ctypes
            _library = ctypes.WinDLL(str(dll))
    import sqlite3
    # This project requires a patched mainline version rather than attempting to
    # infer whether an older distribution carries the WAL-reset backport.
    if sqlite3.sqlite_version_info < (3, 51, 3):
        raise RuntimeError("SQLite 3.51.3+ is required. On Windows run scripts/install_sqlite_runtime.py in the project virtual environment, then restart Python.")
    return sqlite3.sqlite_version
