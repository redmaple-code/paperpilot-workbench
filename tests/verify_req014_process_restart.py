"""Force-kill an isolated app after local provider receipt; verify recovery without resend."""
import argparse,hashlib,json,socket,sqlite3,subprocess,sys,tempfile,threading,time
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parents[1];SECRET='isolated-temporary-test-credential';received=threading.Event();release=threading.Event();calls=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length','0')));calls.append(self.headers.get('Authorization')==f'Bearer {SECRET}')
  if len(calls)==2:received.set();release.wait(45)
  data=json.dumps({'choices':[{'message':{'content':'Local test response.'}}],'usage':{'prompt_tokens':10,'completion_tokens':5}}).encode()
  try:self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
  except (OSError,BrokenPipeError):pass

def port():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
provider=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=provider.serve_forever,daemon=True);thread.start();session=requests.Session();session.trust_env=False;proc=None;temp_mgr=None
try:
 temp_mgr=tempfile.TemporaryDirectory(prefix='paperpilot-force-restart-',ignore_cleanup_errors=True)
 temp=temp_mgr.name
 root=Path(temp);app_port=port();base=f'http://127.0.0.1:{app_port}';provider_url=f'http://127.0.0.1:{provider.server_port}/v1'
 def launch():
  child=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'tests/req014_restart_server.py'),'--root',str(root),'--port',str(app_port)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  end=time.monotonic()+30
  while time.monotonic()<end:
   if child.poll() is not None:raise AssertionError('isolated application exited during startup')
   try:
    if session.get(base+'/api/health',timeout=1).ok:return child
   except requests.RequestException:pass
   time.sleep(.1)
  child.kill();child.wait();raise AssertionError('isolated application startup timed out')
 def req(method,path,data=None,expected=200):
  r=session.request(method,base+path,json=data,timeout=15)
  assert r.status_code==expected,(path,r.status_code,r.text[:600]);return r.json()
 proc=launch()
 profile=req('POST','/api/ai/profiles',{'name':'临时测试模型','base_url':provider_url,'auth_mode':'temporary','models':['test-model'],'model_limits':{'test-model':{'input_tokens':100000,'output_tokens':10000,'source':'isolated fixture'}},'request_id':'restart-profile'})
 req('POST',f"/api/ai/profiles/{profile['id']}/test",{'expected_version':1,'request_id':'restart-test','model_id':'test-model','mode':'chat','consent_to_send':True,'possible_cost_confirmed':True,'temporary_key':SECRET})
 end=time.monotonic()+15
 while time.monotonic()<end:
  status=req('GET',f"/api/ai/profiles/{profile['id']}/tests")[0]['status']
  if status in ('ready','failed'):break
  time.sleep(.1)
 assert status=='ready',status
 profile=req('PUT',f"/api/ai/profiles/{profile['id']}",{'base_version':1,'changes':{'status':'enabled'}})
 pid=req('POST','/api/projects',{'name':'强杀测试论文'})['project']['id'];workflow=req('GET',f'/api/projects/{pid}/workflow');started=req('POST',f'/api/projects/{pid}/workflow/commands',{'command':'start','request_id':'restart-start','base_version':workflow['version']});req('POST',f'/api/projects/{pid}/human-revision',{'workflow_version':started['version']})
 revision=req('GET',f'/api/projects/{pid}')['project']['project_revision'];req('PUT',f'/api/projects/{pid}/ai-binding',{'profile_id':profile['id'],'model_id':'test-model','base_project_revision':revision})
 paper=req('GET',f'/api/projects/{pid}');file=paper['files'][0];before=file['content'];start=before.index('Introduction')
 preview=req('POST',f'/api/projects/{pid}/ai-context',{'action':'edit','target_kind':'file','target_path':file['path'],'content':before,'selection':'Introduction','prompt':'Edit this heading.'})
 job=req('POST',f'/api/projects/{pid}/ai-jobs',{'action':'edit','request_id':'restart-job','prompt':'Edit this heading.','selection':'Introduction','scope':{'targets':[{'kind':'file','path':file['path'],'start':start,'end':start+len('Introduction')}]},'base_file_versions':{file['path']:file['version']},'context_version':preview['version'],'temporary_key':SECRET},expected=202)['job']
 assert received.wait(20),'provider did not receive business request'
 live=req('GET',f"/api/projects/{pid}/ai-jobs/{job['id']}")['job'];assert live['status']=='running' and live['scope']['egress_state']=='sending'
 killed_pid=proc.pid;proc.kill();proc.wait(timeout=10);proc=None
 proc=launch();release.set()
 recovered=req('GET',f"/api/projects/{pid}/ai-jobs/{job['id']}")['job'];assert recovered['status']=='failed' and recovered['error_code']=='interrupted' and recovered['scope']['egress_state']=='unknown'
 assert len(calls)==2 and all(calls);assert req('GET',f'/api/projects/{pid}')['files'][0]['content']==before and not recovered['candidate_id']
 response=session.post(base+f"/api/projects/{pid}/ai-jobs/{job['id']}/retry",json={'request_id':'retry-without-confirm'},timeout=10);assert response.status_code==409 and response.json()['detail']['code']=='duplicate_cost_confirmation_required'
 response=session.post(base+f"/api/projects/{pid}/ai-jobs/{job['id']}/retry",json={'request_id':'retry-without-key','duplicate_cost_confirmed':True},timeout=10);assert response.status_code==409 and response.json()['detail']['code']=='temporary_credential_required'
 time.sleep(2);assert len(calls)==2
 conn=sqlite3.connect(root/'test.db');assert conn.execute('SELECT COUNT(*) FROM ai_jobs WHERE project_id=?',(pid,)).fetchone()[0]==1;conn.close()
 retry=req('POST',f"/api/projects/{pid}/ai-jobs/{job['id']}/retry",{'request_id':'retry-confirmed','duplicate_cost_confirmed':True,'temporary_key':SECRET},expected=202)['job']
 end=time.monotonic()+15
 while time.monotonic()<end:
  retried=req('GET',f"/api/projects/{pid}/ai-jobs/{retry['id']}")['job']
  if retried['status'] not in ('running','cancel_requested'):break
  time.sleep(.1)
 assert retried['status']=='succeeded' and retried['candidate_id'] and len(calls)==3 and all(calls)
 assert req('GET',f'/api/projects/{pid}')['files'][0]['content']==before
 conn=sqlite3.connect(root/'test.db');dump='\n'.join(conn.iterdump());conn.close();assert SECRET not in dump
 files=['app.py','paperpilot/services/ai_jobs.py','paperpilot/services/provider_runtime.py','paperpilot/services/provider_config.py','paperpilot/services/credential_vault.py','paperpilot/adapters/openai_compat.py','tests/req014_restart_server.py','tests/verify_req014_process_restart.py'];report={'passed':True,'time':datetime.now(timezone.utc).isoformat(),'isolated_database':True,'actual_forced_termination':True,'killed_process_id':killed_pid,'local_provider_only':True,'checks':['real provider received business request before termination','restarted task marked interrupted and result unknown','no automatic resend during two-second post-restart observation','explicit duplicate-cost confirmation required','temporary credential must be reentered','confirmed retry produces candidate without changing original content','temporary test key absent from database dump'],'source_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files}}
 (ROOT/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-014-PROCESS-RESTART.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'passed':True,'checks':len(report['checks'])}))
finally:
 release.set()
 if proc and proc.poll() is None:proc.kill();proc.wait(timeout=10)
 if temp_mgr:temp_mgr.cleanup()
 provider.shutdown();provider.server_close();thread.join(timeout=3)
