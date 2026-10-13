#!/usr/bin/env python3
"""Bundle the local preview for Review/offline use, without external resources."""
import base64
import hashlib
import importlib.util
import shutil
from pathlib import Path


HERE = Path(__file__).resolve().parent
FILES = HERE.parents[1]/'packages/runneos-theme/files'
spec = importlib.util.spec_from_file_location('theme_generator', FILES/'generate.py')
theme = importlib.util.module_from_spec(spec);spec.loader.exec_module(theme)
theme.generate(FILES/'tokens.json', HERE)
brand=HERE/'brand';brand.mkdir(exist_ok=True)
for name in ('wordmark-carbon.png','wordmark-cloud.png','launcher-carbon.png','launcher-cloud.png'):
    shutil.copy2(FILES/'brand'/name,brand/name)
script = (HERE/'preview.js').read_text()
digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
html = (HERE/'index.html').read_text()
html = html.replace("default-src 'self'; script-src 'self'", f"default-src 'none'; script-src 'sha256-{digest}'")
html = html.replace('<link rel="stylesheet" href="tokens.css"><link rel="stylesheet" href="preview.css">',
                    '<style>'+ (HERE/'tokens.css').read_text()+'\n'+(HERE/'preview.css').read_text()+'</style>')
html = html.replace('  <script src="preview.js" defer></script>\n', '')
html = html.replace('</body>', '<script>'+script+'</script>\n</body>')
for image in brand.glob('*.png'):
    encoded=base64.b64encode(image.read_bytes()).decode()
    html=html.replace('src="brand/'+image.name+'"','src="data:image/png;base64,'+encoded+'"')
(HERE/'preview.html').write_text(html)
print('Offline preview built:', HERE/'preview.html')
