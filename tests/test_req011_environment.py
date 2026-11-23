import copy
import sqlite3

import pytest

import app
from paperpilot.services import build_environment, build_snapshots
from tests.test_req011_builds import _project
from tests.test_req011_restore import setup_history, persisted, commit
from tests.test_req011_sources_queue import fake_compiler


def configured(monkeypatch):
    monkeypatch.setenv('PWA_TEX_SANDBOX_IMAGE','example-tex:local')
    monkeypatch.setenv('PWA_DOCKER_EXECUTABLE','docker')
    monkeypatch.setenv('PWA_LATEX_ENGINE','pdflatex')
    monkeypatch.setenv('PWA_BIB_ENGINE','auto')
    monkeypatch.setenv('PWA_LATEX_TIMEOUT_SECONDS','60')
    monkeypatch.setenv('PWA_TEX_PIDS_LIMIT','128')
    monkeypatch.setenv('PWA_TEX_MEMORY_MB','2048')
    monkeypatch.setenv('PWA_TEX_CPU_LIMIT','2')


def view(client,pid,job):
    response=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'restore_build_environment':True})
    assert response.status_code==200,response.text
    return response.json()


def prepare_environment(client,pid,job,preview,request='environment'):
    return client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':request,'restore_build_environment':True,'preview_fingerprint':preview['preview_fingerprint']})


def test_reviewed_environment_pins_image_and_can_reset_to_local_default(client,monkeypatch):
    configured(monkeypatch);image='sha256:'+'a'*64
    monkeypatch.setattr(build_environment,'_inspect_image',lambda docker,target:image)
    pid,job,_,_=setup_history(client,monkeypatch);other=_project(client)
    monkeypatch.setenv('PWA_LATEX_ENGINE','xelatex')
    before=persisted(client,pid);review=view(client,pid,job);env=review['configuration']['environment_review']
    assert review['can_prepare'] and env['available'] and not env['historical_image_verified'] and '无法证明' in env['message']
    assert persisted(client,pid)==before
    staged=prepare_environment(client,pid,job,review);assert staged.status_code==200,staged.text
    assert not client.get(f'/api/projects/{pid}/build-environment').json()['overridden']
    seen=[];fake=fake_compiler([])
    def compiler(*args,**kwargs):seen.append(kwargs['profile']);return fake(*args,**kwargs)
    monkeypatch.setattr(app,'compile_project_files',compiler)
    result=commit(client,pid,staged.json());assert result.status_code==200,result.text
    effective=client.get(f'/api/projects/{pid}/build-environment').json()
    assert effective['overridden'] and effective['effective']['PWA_LATEX_ENGINE']=='pdflatex'
    assert effective['effective']['PWA_TEX_IMAGE_ID']==image and seen[-1]['PWA_TEX_IMAGE_ID']==image
    assert not client.get(f'/api/projects/{other}/build-environment').json()['overridden']
    job_data=client.get(result.json()['status_url']).json();assert job_data['status']=='succeeded' and job_data['current']
    snapshot=client.get(f"/api/projects/{pid}/build-snapshots/{job_data['snapshot_id']}").json()
    assert snapshot['manifest']['build_profile']['PWA_TEX_IMAGE_ID']==image
    revision=client.get(f'/api/projects/{pid}').json()['project']['project_revision']
    reset=client.post(f'/api/projects/{pid}/build-environment/reset',json={'request_id':'reset-env','expected_project_revision':revision})
    assert reset.status_code==200 and reset.json()['reset']
    assert not client.get(f'/api/projects/{pid}/build-environment').json()['overridden']
    assert client.get(f'/api/projects/{pid}/build-environment').json()['effective']['PWA_LATEX_ENGINE']=='xelatex'
    assert not client.get(result.json()['status_url']).json()['current']
    assert client.post(f'/api/projects/{pid}/build-environment/reset',json={'request_id':'reset-env','expected_project_revision':revision}).json()['idempotent_replay']
    conflict=client.post(f'/api/projects/{pid}/build-environment/reset',json={'request_id':'reset-env','expected_project_revision':revision+1})
    assert conflict.status_code==409 and conflict.json()['detail']['code']=='build_profile_request_conflict'


def test_image_tag_change_rejects_preview_and_commit_without_writes(client,monkeypatch):
    configured(monkeypatch);image={'id':'sha256:'+'a'*64}
    monkeypatch.setattr(build_environment,'_inspect_image',lambda *args:image['id'])
    pid,job,_,_=setup_history(client,monkeypatch)
    first=view(client,pid,job);image['id']='sha256:'+'b'*64
    denied=prepare_environment(client,pid,job,first,'stale-image')
    assert denied.status_code==409 and denied.json()['detail']['code']=='build_restore_preview_stale'
    image['id']='sha256:'+'a'*64;second=view(client,pid,job);staged=prepare_environment(client,pid,job,second,'candidate-image').json()
    before=persisted(client,pid);image['id']='sha256:'+'b'*64
    denied=commit(client,pid,staged)
    assert denied.status_code==409 and denied.json()['detail']['code']=='build_restore_environment_changed'
    assert persisted(client,pid)==before


def test_environment_apply_failure_rolls_back_everything(client,monkeypatch):
    configured(monkeypatch);monkeypatch.setattr(build_environment,'_inspect_image',lambda *args:'sha256:'+'a'*64)
    pid,job,_,_=setup_history(client,monkeypatch);review=view(client,pid,job);staged=prepare_environment(client,pid,job,review).json();before=persisted(client,pid)
    monkeypatch.setattr(build_environment,'apply',lambda *a,**k: (_ for _ in ()).throw(sqlite3.OperationalError('injected failure')))
    denied=commit(client,pid,staged)
    assert denied.status_code==503 and persisted(client,pid)==before


def test_reset_rejects_stale_revision_and_read_only_project(client,monkeypatch):
    configured(monkeypatch);monkeypatch.setattr(build_environment,'_inspect_image',lambda *args:'sha256:'+'a'*64)
    pid,job,_,_=setup_history(client,monkeypatch);review=view(client,pid,job);staged=prepare_environment(client,pid,job,review).json()
    assert commit(client,pid,staged).status_code==200
    revision=client.get(f'/api/projects/{pid}').json()['project']['project_revision'];before=persisted(client,pid)
    denied=client.post(f'/api/projects/{pid}/build-environment/reset',json={'request_id':'stale-reset','expected_project_revision':revision-1})
    assert denied.status_code==409 and denied.json()['detail']['code']=='project_revision_conflict' and persisted(client,pid)==before
    conn=app.db()
    with conn:conn.execute("UPDATE projects SET lifecycle_state='published' WHERE id=?",(pid,))
    conn.close()
    denied=client.post(f'/api/projects/{pid}/build-environment/reset',json={'request_id':'published-reset','expected_project_revision':revision})
    assert denied.status_code==409 and denied.json()['detail']['code']=='build_profile_read_only'
    assert client.get(f'/api/projects/{pid}/build-environment').json()['overridden']


def test_unavailable_unsafe_and_pinned_historical_environments(monkeypatch):
    configured(monkeypatch);current=build_snapshots.build_profile();profile=copy.deepcopy(current)
    def must_not_inspect(*_):pytest.fail('unsafe settings must be rejected before execution')
    monkeypatch.setattr(build_environment,'_inspect_image',must_not_inspect)
    for key,value in [('PWA_DOCKER_EXECUTABLE','other-program'),('PWA_TEX_SANDBOX_IMAGE','--privileged'),('PWA_TEX_MEMORY_MB','9000'),('PWA_LATEX_ENGINE','sh')]:
        changed=copy.deepcopy(profile);changed[key]=value
        result=build_environment.inspect_historical(changed,current)
        assert not result['available']
        if key=='PWA_DOCKER_EXECUTABLE':assert '取消历史环境选择' in result['message']
    image='sha256:'+'b'*64;targets=[]
    monkeypatch.setattr(build_environment,'_inspect_image',lambda docker,target:targets.append(target) or image)
    profile['PWA_TEX_IMAGE_ID']=image
    result=build_environment.inspect_historical(profile,current)
    assert result['available'] and result['historical_image_verified'] and targets==[image]
    legacy=copy.deepcopy(profile);legacy.pop('source_map_format')
    result=build_environment.inspect_historical(legacy,current)
    assert result['available'] and '无法双向定位' in result['message'] and 'source_map_format' not in result['profile']
    legacy['source_map_format']=None;result=build_environment.inspect_historical(legacy,current)
    assert result['available'] and '无法双向定位' in result['message'] and result['profile']['source_map_format'] is None
    monkeypatch.setattr(build_environment,'_inspect_image',lambda *_:None)
    assert not build_environment.inspect_historical(profile,current)['available']


def test_image_inspection_uses_bounded_local_command_and_minimal_environment(monkeypatch):
    from types import SimpleNamespace
    calls=[];monkeypatch.setenv('PAPERPILOT_SYNTHETIC_SECRET','synthetic-not-for-child')
    def run(args,**kwargs):
        calls.append((args,kwargs));return SimpleNamespace(returncode=0,stdout='sha256:'+'a'*64+'\n')
    monkeypatch.setattr(build_environment.subprocess,'run',run)
    assert build_environment._inspect_image('docker','example:local')=='sha256:'+'a'*64
    args,options=calls[0]
    assert args==['docker','image','inspect','--format','{{.Id}}','example:local']
    assert options['timeout']==5 and 'PAPERPILOT_SYNTHETIC_SECRET' not in options['env'] and not options.get('shell',False)


def test_compiler_and_location_use_pinned_image_without_tag_fallback(tmp_path,monkeypatch):
    from paperpilot.adapters import latex
    from paperpilot.services import tex_runtime
    configured(monkeypatch);profile=build_snapshots.build_profile();image='sha256:'+'a'*64;profile['PWA_TEX_IMAGE_ID']=image
    project=tmp_path/'paper';work=project/'run';seen=[]
    def capture(_docker,image_arg,*args,**kwargs):
        seen.append(image_arg);raise OSError('stop before launching container')
    monkeypatch.setattr(latex,'_container_command',capture)
    monkeypatch.setattr(tex_runtime,'cleanup',lambda *args,**kwargs:{'cleanup_verified':True})
    result=latex.compile_project_files([{'path':'main.tex','content':r'\documentclass{article}\begin{document}x\end{document}'}],project,lambda x:x,work,job_id='a'*32,profile=profile)
    assert not result['ok'] and seen==[image]
    result=latex.query_synctex(work,project,'a'*32,['view'],profile)
    assert not result['ok'] and seen==[image,image]
    profile['PWA_TEX_IMAGE_ID']='invalid';seen.clear()
    result=latex.compile_project_files([{'path':'main.tex','content':'x'}],project,lambda x:x,work,job_id='b'*32,profile=profile)
    assert result['code']=='sandbox_image_identity_invalid' and not seen


def test_historical_docker_executable_is_not_run_after_local_configuration_changes(tmp_path,monkeypatch):
    from paperpilot.adapters import latex
    from paperpilot.services import tex_runtime
    configured(monkeypatch);profile=build_snapshots.build_profile();profile['PWA_DOCKER_EXECUTABLE']='old-docker'
    monkeypatch.setattr(tex_runtime,'reserve',lambda *args,**kwargs:pytest.fail('must not start historical executable'))
    project=tmp_path/'paper';work=project/'run'
    result=latex.compile_project_files([{'path':'main.tex','content':'x'}],project,lambda x:x,work,job_id='c'*32,profile=profile)
    assert result['code']=='sandbox_runtime_changed' and '当前环境' in result['log']
    result=latex.query_synctex(work,project,'c'*32,['view'],profile)
    assert result['code']=='source_map_tool_unavailable' and '旧PDF仍可阅读' in result['message']
