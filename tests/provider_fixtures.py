"""Persisted test results for isolated consumer tests; never cloud test evidence."""
import uuid
import app
import os
from paperpilot.services import provider_runtime


def seed_generation_fixture(profile_id,model,modes=('chat',)):
    conn=app.db()
    try:
        with conn:
            config=provider_runtime.configuration(conn,profile_id)
            for mode in modes:
                tid='fixture-'+uuid.uuid4().hex;t=provider_runtime.stamp()
                conn.execute("INSERT INTO ai_provider_tests(id,profile_id,config_revision,config_fingerprint,model_id,mode,status,request_id,request_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,'ready',?,?,?,?)",
                    (tid,profile_id,config['config_revision'],provider_runtime.fingerprint(config),model,mode,tid,'isolated-consumer-fixture',t,t))
    finally:conn.close()


def install_default_provider_fixture():
    """Authoring consumer tests use a registered default; transport is still mocked explicitly."""
    from paperpilot.services import provider_config
    conn=app.db()
    try:
        with conn:
            p=provider_config.create_profile(conn,{'name':'Isolated authoring fixture','base_url':'http://127.0.0.1:1/v1','auth_mode':'none',
                'models':[os.environ.get('PWA_AI_MODEL','deepseek-chat')], 'model_limits':{os.environ.get('PWA_AI_MODEL','deepseek-chat'):{'input_tokens':1000000,'output_tokens':100000,'source':'isolated consumer test fixture'}},'request_id':'fixture-default'},'fixture-default')
        seed_generation_fixture(p['id'],p['models'][0])
        with conn:
            enabled=provider_config.update_profile(conn,p['id'],p['current_version'],{'status':'enabled'})
            provider_runtime.set_default(conn,p['id'],enabled['current_version'],p['models'][0],0)
    finally:conn.close()


def logic_preview_payload(client,project_id,payload):
    preview=client.post(f'/api/projects/{project_id}/logic/suggestions/preview',json=payload)
    assert preview.status_code==200,preview.text
    return payload|{'preview_fingerprint':preview.json()['preview_fingerprint'],'consent_to_send':True}


def authoring_preflight_payload(client,project_id,payload):
    project=client.get(f'/api/projects/{project_id}').json()
    kind=payload['target_kind'];path=payload.get('target_path','')
    content=project['outline'] if kind=='outline' else next(f['content'] for f in project['files'] if f['path']==path)
    modification='instruction' in payload
    start=payload.get('start_offset',0) if modification else payload.get('anchor_start',0)
    end=payload.get('end_offset',0) if modification else payload.get('anchor_end',0)
    preview=client.post(f'/api/projects/{project_id}/ai-context',json={'action':payload.get('action','rewrite' if modification else 'continue'),
        'target_kind':kind,'target_path':path,'content':content,'selection':content[start:end],
        'prompt':payload['instruction'] if modification else payload.get('prompt') or payload['goal']})
    assert preview.status_code==200,preview.text
    return payload|{'context_version':preview.json()['version']}
