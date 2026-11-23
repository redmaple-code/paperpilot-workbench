"""Pause immediately after OA state commit and before cleanup."""
import sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from paperpilot.domain.schemas import ImportConfirmIn
app.DB_PATH=Path(sys.argv[1]);app.PROJECTS_DIR=Path(sys.argv[2]);pid,iid,action,marker=sys.argv[3:]
def pause(*_):
 Path(marker).write_text('committed',encoding='utf-8');time.sleep(120);return True
app._oa_cleanup_after_commit=pause
if action=='confirm':app.confirm_oa_import(pid,iid,ImportConfirmIn(keep_attachment=False))
else:app.cancel_oa_import(pid,iid)
