"""Stop only this lab's exact name, image and label; retain its container evidence."""
import json
from pathlib import Path
import subprocess
import sys

name, image, label, out = sys.argv[1:]
result = {'cleanup_confirmed': False, 'owned_container': name}
try:
    row = subprocess.run(['docker', 'inspect', '--type', 'container', name], capture_output=True, text=True, timeout=8)
    if row.returncode:
        expected = {f'error: no such object: {name}', f'error: no such container: {name}',
                    f'error response from daemon: no such container: {name}', f'error response from daemon: no such object: {name}'}
        if row.returncode == 1 and row.stdout.strip() in ('[]', '') and row.stderr.strip().casefold() in expected:
            result.update(cleanup_confirmed=True, disposition='never_created')
        else:
            raise RuntimeError('container_inspection_unknown')
    else:
        values = json.loads(row.stdout)
        if len(values) != 1:
            raise RuntimeError('unexpected_container_count')
        value = values[0]
        if value.get('Name') != '/' + name or value.get('Config', {}).get('Image') != image or value.get('Config', {}).get('Labels', {}).get('servicevoice.phone_lab') != label:
            raise RuntimeError('container_identity_mismatch')
        exact = value['Id']
        if value.get('State', {}).get('Running'):
            subprocess.run(['docker', 'stop', '--time', '3', exact], check=True, capture_output=True, timeout=8)
        check = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', exact], check=True, capture_output=True, text=True, timeout=5)
        if check.stdout.strip() != 'false':
            raise RuntimeError('owned_container_still_running')
        result.update(cleanup_confirmed=True, disposition='stopped_retained')
except Exception as error:
    result['failure'] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
Path(out, 'cleanup.json').write_text(json.dumps(result, indent=2) + '\n')
raise SystemExit(0 if result['cleanup_confirmed'] else 1)
