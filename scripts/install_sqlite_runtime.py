"""Install a pinned SQLite DLL only inside this project and its virtualenv.

The startup hook makes test subprocesses use the same patched SQLite library.
No system Python file, global PATH, or other virtual environment is changed.
"""
import hashlib
import io
import json
from pathlib import Path
import platform
import sys
import sysconfig
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.sqlite.org/2026/sqlite-dll-win-x64-3540100.zip"
ARCHIVE_SHA3 = "c2d09c8babb9399277055c057c97a53f1ffd7c5f11d0c1f2118ffdfa39f2daa4"
DLL_SHA256 = "4b59d3cded9dc1608544160f3b4b10ac974c6ac0d6ffc23064e5f4c8dbd05aea"


def main():
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("This pinned installer is for Windows x64. Use a supported SQLite 3.51.3+ runtime on other systems.")
    if sys.prefix == sys.base_prefix:
        raise SystemExit("Run this inside the project's virtual environment; system Python is never modified.")
    target = ROOT / ".local/sqlite"
    target.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(URL, timeout=25) as response:
        archive = response.read(4*1024*1024+1)
    if hashlib.sha3_256(archive).hexdigest() != ARCHIVE_SHA3:
        raise SystemExit("SQLite download failed its official pinned SHA3 check; nothing installed.")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = [name for name in bundle.namelist() if Path(name).name == "sqlite3.dll"]
        if len(names) != 1: raise SystemExit("Unexpected archive layout.")
        dll = bundle.read(names[0])
    if hashlib.sha256(dll).hexdigest() != DLL_SHA256: raise SystemExit("Unexpected SQLite DLL hash.")
    destination = target / "sqlite3.dll"
    if not destination.exists() or destination.read_bytes() != dll:
        destination.write_bytes(dll)
    hook = Path(sysconfig.get_path("purelib")) / "servicevoice_sqlite.pth"
    # json.dumps emits a Python-compatible quoted Unicode path here; this is a
    # Python startup file, never interpolated into a shell command.
    line = "import sys; sys.path.insert(0, " + json.dumps(str(ROOT)) + "); import runtime_sqlite; runtime_sqlite.ensure_sqlite()\n"
    hook.write_text(line, encoding="utf-8")
    (target / "manifest.json").write_text(json.dumps({"version": "3.54.1", "source": URL,
        "archive_sha3_256": ARCHIVE_SHA3, "dll_sha256": DLL_SHA256,
        "scope": "project DLL plus this virtualenv startup only"}, indent=2)+"\n", encoding="utf-8")
    print("Installed verified SQLite 3.54.1 for this project/virtualenv. Restart running Python processes to use it.")


if __name__ == "__main__":
    main()
