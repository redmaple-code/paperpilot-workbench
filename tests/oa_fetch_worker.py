"""Subprocess fixture for verifying interrupted OA fetch ownership."""
import sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
app.DB_PATH=Path(sys.argv[1]);app.PROJECTS_DIR=Path(sys.argv[2]);marker=Path(sys.argv[5])
def held_fetch(_url):
 marker.write_text('started',encoding='utf-8')
 time.sleep(120)
 return b'unexpected','text/html','test'
app.oa_imports.fetch_public=held_fetch
app._fetch_oa_import(sys.argv[3],sys.argv[4])
