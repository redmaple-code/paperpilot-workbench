"""Child server for isolated forced-restart verification."""
import argparse,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app, uvicorn
parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--port',type=int,required=True);args=parser.parse_args()
root=Path(args.root);app.DB_PATH=root/'test.db';app.PROJECTS_DIR=root/'projects';app.PROJECTS_DIR.mkdir(exist_ok=True)
uvicorn.run(app.app,host='127.0.0.1',port=args.port,log_level='error')
