"""Crash after source file is written, before preview is committed."""
import sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
app.DB_PATH=Path(sys.argv[1]);app.PROJECTS_DIR=Path(sys.argv[2]);marker=Path(sys.argv[5])
app.oa_imports.fetch_public=lambda _:(b'temporary attachment','text/html','fixture')
def held_preview(*_):
 marker.write_text('written',encoding='utf-8');time.sleep(120);return {}
app.oa_imports.extract_preview=held_preview
app._fetch_oa_import(sys.argv[3],sys.argv[4])
