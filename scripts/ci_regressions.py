"""CI-only offline partitioning; retain every test and stream failure evidence."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_sqlite import ensure_sqlite
from evals.offline import code_test_input_manifest


def main():
    ensure_sqlite()
    suite = os.environ['CI_SUITE']
    before = code_test_input_manifest(ROOT)
    if suite == 'node':
        command = [shutil.which('node'), '--test', '--test-reporter=tap',
                   *[str(p.relative_to(ROOT)) for p in sorted((ROOT/'tests').glob('*.mjs'))]]
    elif suite in {'0', '1', '2', '3'}:
        files = sorted((ROOT/'tests').glob('test_*.py'))
        groups = [files[i::4] for i in range(4)]
        assert sorted(p for group in groups for p in group) == files
        selected = groups[int(suite)]
        assert selected
        command = [sys.executable, '-u', '-m', 'pytest', '-q', '--durations=15',
                   *[str(p.relative_to(ROOT)) for p in selected]]
    else:
        raise ValueError('unknown_ci_suite')
    print(f'Offline suite {suite}: {len(command)-3} command arguments; no provider credentials configured.', flush=True)
    result = subprocess.run(command, cwd=ROOT, timeout=900, check=False,
                            env={**os.environ, 'SERVICEVOICE_LANGSMITH_ENABLED': 'off'})
    if before != code_test_input_manifest(ROOT):
        print('Source inputs changed during the test run.', flush=True)
        return 1
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
