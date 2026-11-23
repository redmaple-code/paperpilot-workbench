"""Two isolated app processes share a live provider-generation test."""
import hashlib,json,socket,subprocess,sys,tempfile,threading,time
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1];received=threading.Event();release=threading.Event();calls=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length','0')));calls.append(1);received.set();release.wait(30)
  data=json.dumps({'choices':[{'message':{'content':'OK'}}]}).encode()
  try:self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
  except OSError:pass

def port():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();session=requests.Session();session.trust_env=False;children=[];tmp=None
try:
 tmp=tempfile.TemporaryDirectory(prefix='paperpilot-owner-test-',ignore_cleanup_errors=True);root=Path(tmp.name)
 def launch(chosen):
  child=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'tests/req014_restart_server.py'),'--root',str(root),'--port',str(chosen)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);children.append(child);end=time.monotonic()+30
  while time.monotonic()<end:
   if child.poll() is not None:raise AssertionError('app startup failed')
   try:
    if session.get(f'http://127.0.0.1:{chosen}/api/health',timeout=1).ok:return
   except requests.RequestException:pass
   time.sleep(.1)
  raise AssertionError('app startup timed out')
 a=port();launch(a);base=f'http://127.0.0.1:{a}'
 def req(method,path,data=None):
  r=session.request(method,base+path,json=data,timeout=10);assert r.ok,(r.status_code,r.text[:500]);return r.json()
 profile=req('POST','/api/ai/profiles',{'name':'本地慢测试','base_url':f'http://127.0.0.1:{server.server_port}/v1','auth_mode':'none','models':['m'],'request_id':'live-profile'})
 req('POST',f"/api/ai/profiles/{profile['id']}/test",{'expected_version':1,'request_id':'live-provider-test','model_id':'m','mode':'chat','consent_to_send':True,'possible_cost_confirmed':True})
 assert received.wait(15),'test was not sent';b=port();launch(b)
 second=session.get(f"http://127.0.0.1:{b}/api/ai/profiles/{profile['id']}/tests",timeout=10);assert second.ok
 state=second.json()[0];assert state['status']=='running' and state['recovery_state']=='alive',state
 release.set();end=time.monotonic()+15
 while time.monotonic()<end:
  state=req('GET',f"/api/ai/profiles/{profile['id']}/tests")[0]
  if state['status'] not in ('queued','running'):break
  time.sleep(.1)
 assert state['status']=='ready' and len(calls)==1,state
 received.clear();release.clear()
 req('POST',f"/api/ai/profiles/{profile['id']}/test",{'expected_version':1,'request_id':'dead-provider-test','model_id':'m','mode':'chat','consent_to_send':True,'possible_cost_confirmed':True})
 assert received.wait(15) and len(calls)==2
 children[0].kill();children[0].wait(timeout=10)
 c=port();launch(c)
 interrupted=session.get(f"http://127.0.0.1:{c}/api/ai/profiles/{profile['id']}/tests",timeout=10).json()
 dead=next(item for item in interrupted if item['request_id']=='dead-provider-test')
 assert dead['status']=='failed' and dead['error_code']=='interrupted_no_resend',dead
 release.set();time.sleep(.3);assert len(calls)==2
 files=['app.py','paperpilot/core/database.py','paperpilot/services/provider_runtime.py','paperpilot/services/tex_runtime.py','tests/req014_restart_server.py','tests/verify_req014_provider_live_owner.py'];report={'passed':True,'time':datetime.now(timezone.utc).isoformat(),'isolated_database':True,'checks':['second app preserves live provider test','original test completes after second startup','dead owner test marked interrupted without automatic resend'],'source_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files}}
 (ROOT/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-014-PROVIDER-LIVE-OWNER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'passed':True,'checks':len(report['checks'])}))
finally:
 release.set()
 for child in children:
  if child.poll() is None:child.kill();child.wait(timeout=10)
 if tmp:tmp.cleanup()
 server.shutdown();server.server_close();thread.join(timeout=3)
