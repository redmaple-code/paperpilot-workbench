"""Isolated server fixture for process-lifetime session storage tests."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,uvicorn
app.DB_PATH=Path(sys.argv[1]);app.PROJECTS_DIR=Path(sys.argv[2]);app.PROJECTS_DIR.mkdir(exist_ok=True);app.init_db()
class Provider:
 name='fixture'
 def search(self,query,filters,cursor,limit):return {'results':[{'id':'https://example.org/'+query,'title':query}],'next_cursor':None}
app.academic_search.provider_from_env=lambda:Provider()
uvicorn.run(app.app,host='127.0.0.1',port=int(sys.argv[3]),log_level='error')
