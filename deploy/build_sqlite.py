"""Build the pinned official SQLite amalgamation; never use an unchecked archive."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import urllib.request
import zipfile

URL = 'https://sqlite.org/2026/sqlite-amalgamation-3540100.zip'
SHA3_256 = '92281c86e2957f9b9305890f9ac04204caa7b677cc868dc326d942689df28e30'


def main():
    with urllib.request.urlopen(URL, timeout=30) as response:
        archive = response.read(4_000_001)
    if len(archive) > 4_000_000 or hashlib.sha3_256(archive).hexdigest() != SHA3_256:
        raise RuntimeError('Official SQLite archive integrity verification failed.')
    source = Path('/build/sqlite')
    target = Path('/opt/sqlite/lib')
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        for name in ('sqlite3.c', 'sqlite3.h', 'sqlite3ext.h'):
            (source / name).write_bytes(package.read('sqlite-amalgamation-3540100/' + name))
    flags = ['-O2', '-fPIC', '-shared', '-Wl,-soname,libsqlite3.so.0',
             '-DSQLITE_THREADSAFE=1', '-DSQLITE_ENABLE_COLUMN_METADATA',
             '-DSQLITE_ENABLE_FTS5', '-DSQLITE_ENABLE_RTREE', '-DSQLITE_ENABLE_MATH_FUNCTIONS']
    binary = target / 'libsqlite3.so.0'
    subprocess.run(['gcc', *flags, str(source / 'sqlite3.c'), '-o', str(binary), '-lm'], check=True, timeout=180)
    compiler = subprocess.check_output(['gcc', '-dumpfullversion'], text=True).strip()
    manifest = {'version': '3.54.1', 'source_url': URL, 'source_sha3_256': SHA3_256,
                'compiler_version': compiler, 'compiler_flags': flags,
                'library_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}
    (target.parent / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    main()
