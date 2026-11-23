import base64
import io
import json
import zipfile

import app


def ok(response):
    assert response.is_success, response.text
    return response.json()


def candidate(view, name):
    return next(item for item in view['items'] if item['canonical']==name)


def test_staged_author_batch_matches_library_and_can_create_project(client):
    reference=ok(client.post('/api/references',json={'title':'Adaptive Flux Filter','doi':'10.1000/filter'}))
    base='/api/manuscript-imports'
    batch=ok(client.post(base,json={'request_id':'terms-batch','name':'Term fixture','target_mode':'new'}))
    url=f"{base}/{batch['id']}"
    def upload(name,text):
        return ok(client.post(url+'/sources',json={'request_id':name,'filename':name,
                         'content_base64':base64.b64encode(text.encode()).decode()}))

    upload('paper.md','# Methods\nAdaptive Flux Filter (AFF) uses WENO-Z and WENO-Z+.\n')
    upload('paper.bib','@article{filter, title={Adaptive Flux Filter}, doi={10.1000/filter}}')
    preview=ok(client.post(url+'/terminology/preview',json={}))
    assert preview['project_id'] is None
    assert preview['reference_ids']==[reference['id']]
    assert preview['summary']['missing_fulltext']==1
    assert {'WENO-Z','WENO-Z+'}.issubset({item['canonical'] for item in preview['items']})
    aff=candidate(preview,'AFF')
    assert aff['aliases']==['Adaptive Flux Filter']
    body={'fingerprint':preview['fingerprint'],'request_id':'accept-filter','decisions':{aff['id']:{'action':'add'}}}
    result=ok(client.post(url+'/terminology/apply',json=body))
    assert result['project_id']
    assert ok(client.post(url+'/terminology/apply',json=body))['idempotent_replay']
    assert len(ok(client.get(f"/api/projects/{result['project_id']}"))['terms'])==1


def test_reference_version_evidence_merges_and_preserves_source_history(client):
    project=ok(client.post('/api/projects',json={'name':'Fixture'}))['project']['id']
    other=ok(client.post('/api/projects',json={'name':'Other'}))['project']['id']
    base=f'/api/projects/{project}'
    ok(client.put(base+'/files',json={'path':'main.tex','base_version':1,'content':
        'Adaptive Flux Filter (AFF) is used here.\nWe use the AFF with a numerical flux.\n'}))
    old=ok(client.post(base+'/terms',json={'term':'AFF','aliases':'Existing alias'}))
    ref=ok(client.post('/api/references',json={'title':'Adaptive Flux Filter','abstract':'Adaptive Flux Filter (AFF) is used.'}))['id']
    ref_base=base+f'/references/{ref}'
    ok(client.post(ref_base,json={'request_id':'link','reference_version':1,'cite_key':'filter'}))
    comparison=base+'/terminology/comparison'
    first=ok(client.post(comparison+'/preview',json={}))
    assert first['references'][0]['status']=='abstract'
    manuscript_only=ok(client.post(comparison+'/preview',json={'reference_ids':[]}))
    assert 'reference' not in {src['kind'] for src in candidate(manuscript_only,'AFF')['sources']}

    stamp='2026-11-22T00:00:00Z'
    with app.db() as conn:
        conn.execute('''INSERT INTO oa_imports(id,project_id,idempotency_key,access_status,status,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?)''',('oa',project,'fixture-oa','open','imported',stamp,stamp))
        fragment='AFF: A conservative filter applied to the interface flux.'
        conn.execute('''INSERT INTO external_evidence(id,project_id,import_id,fragments_json,confirmed_at,source_version)
                        VALUES(?,?,?,?,?,?)''',('evidence',project,'oa',json.dumps([{'location':'page:3','text':fragment}]),stamp,'fixture-v1'))
    attached=ok(client.post(ref_base+'/attachments',json={'project_id':project,'external_evidence_id':'evidence','base_version':1}))
    assert attached['reference_version']==2
    assert ok(client.get(ref_base+'/terms'))['status']=='abstract'
    assert ok(client.get(f'/api/projects/{other}/references/{ref}/terms?version=2'))['status']=='abstract'
    assert ok(client.get(ref_base+'/terms?version=2'))['status']=='fulltext'
    ok(client.post(ref_base+'/refresh',json={'request_id':'refresh','reference_version':2,'base_version':1}))
    stale=client.post(comparison+'/apply',json={'fingerprint':first['fingerprint'],'request_id':'stale',
                                                'decisions':{candidate(first,'AFF')['id']:{'action':'merge'}}})
    assert stale.status_code==409

    preview=ok(client.post(comparison+'/preview',json={}))
    aff=candidate(preview,'AFF')
    evidence=next(src for src in aff['sources'] if src['location']=='page:3')
    assert evidence['quote']==fragment and evidence['version']==2
    assert {'manuscript','reference'} <= {src['kind'] for src in aff['sources']}
    response=ok(client.post(comparison+'/apply',json={'fingerprint':preview['fingerprint'],'request_id':'merge',
        'decisions':{aff['id']:{'action':'merge','definition_id':evidence['id']}}}))
    term_id=response['items'][0]['id']
    assert term_id==old['id']
    history=ok(client.get(f'{comparison}/{term_id}/history'))['versions']
    assert [entry['version'] for entry in history]==[2,1]
    assert history[0]['snapshot']['definition']=='A conservative filter applied to the interface flux.'
    assert 'Existing alias' in history[0]['snapshot']['aliases']
    assert candidate(ok(client.post(comparison+'/preview',json={})),'AFF')['status']=='covered'


def test_abbreviation_collision_requires_distinct_name_for_separate_term(client):
    pid=ok(client.post('/api/projects',json={'name':'Collision fixture'}))['project']['id']
    base=f'/api/projects/{pid}'
    ok(client.put(base+'/files',json={'path':'main.tex','base_version':1,'content':
        'Dynamic Flux Filter (DFF) appears in the first method.\nDiscrete Flux Filter (DFF) appears in the second method.\n'}))
    old=ok(client.post(base+'/terms',json={'term':'DFF','definition':'Existing first method.'}))
    comparison=base+'/terminology/comparison'
    preview=ok(client.post(comparison+'/preview',json={}))
    item=candidate(preview,'DFF')
    assert item['status']=='conflict'
    assert set(item['aliases'])=={'Dynamic Flux Filter','Discrete Flux Filter'}
    body={'fingerprint':preview['fingerprint'],'request_id':'separate-filter','decisions':{item['id']:{'action':'separate'}}}
    assert client.post(comparison+'/apply',json=body).status_code==422
    proof=next(source['id'] for source in item['sources'] if 'Discrete Flux Filter' in source['quote'])
    body['decisions'][item['id']].update(canonical='DFF (discrete filter)',aliases=['Discrete Flux Filter'],source_ids=[proof])
    result=ok(client.post(comparison+'/apply',json=body))
    assert result['items'][0]['id']!=old['id']
    terms=ok(client.get(base))['terms']
    assert next(term for term in terms if term['id']==old['id'])['definition']=='Existing first method.'
    history=ok(client.get(f"{comparison}/{result['items'][0]['id']}/history"))
    assert [source['id'] for source in history['versions'][0]['sources']]==[proof]


def test_tex_project_only_recognizes_reachable_manuscript_sections(client):
    url='/api/manuscript-imports'
    batch=ok(client.post(url,json={'request_id':'terms-archive','name':'Archive fixture','target_mode':'new'}))
    url+=f"/{batch['id']}"
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:
        archive.writestr('main.tex','\\documentclass{article}\n\\input{sections/methods}\n')
        archive.writestr('sections/methods.tex','\\section{Methods}\nThe method compares WENO-JS and WENO-Z+.')
        archive.writestr('sections/unused.tex','\\section{Archived}\nThe unused method is LEGACY-NAME.')
    ok(client.post(url+'/sources',json={'request_id':'archive','filename':'source.zip','content_base64':base64.b64encode(data.getvalue()).decode()}))
    result=ok(client.post(url+'/terminology/preview',json={}))
    assert 'sections/methods.tex' in candidate(result,'WENO-Z+')['sources'][0]['name']
    assert 'LEGACY-NAME' not in {item['canonical'] for item in result['items']}
