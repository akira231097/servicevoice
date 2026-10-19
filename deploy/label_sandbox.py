"""Label the image's UI copy; never modify the host application's assets."""
from pathlib import Path

page = Path('/app/app/static/index.html')
source = page.read_text()
if source.count('<body>') != 1:
    raise RuntimeError('Sandbox UI insertion point changed.')
banner = '<aside role="note" style="padding:12px 24px;background:#fff1ca;color:#322600;text-align:center;font:600 14px system-ui">Local Docker sandbox · separate data volume. Your host demo is unchanged.</aside>'
page.write_text(source.replace('<body>', '<body>' + banner, 1))
