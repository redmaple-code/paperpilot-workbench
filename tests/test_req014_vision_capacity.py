"""Full vision payload admission with actual local HTTP, never cloud quality evidence."""
import json
import uuid

import pytest

import app
from paperpilot.adapters.visual_vision import analyze
from paperpilot.domain.visual_evidence_schemas import VisualCheckIn
from paperpilot.services import provider_runtime, vision_capacity, visual_evidence
from paperpilot.services.project_errors import ProjectServiceError
from tests.test_req014_provider_runtime import local_provider
from tests.test_req022_assets import project, upload, png


def bounds(**changes):
    return {'input_tokens': 100000, 'output_tokens': 10000, 'source': 'isolated local protocol total capacity',
        'vision': {'tokens_per_image': 2000, 'max_width': 1600, 'max_height': 1600,
            'source': 'isolated high detail per-image upper bound fixture; not real model capacity'}, **changes}


def enabled(client, pid, url, limits):
    response = client.post('/api/ai/profiles', json={'name': 'Vision capacity fixture', 'base_url': url + '/v1',
        'auth_mode': 'none', 'models': ['test-model'], 'capabilities': {'test-model': {'vision_analysis': True, 'price_per_check': 0}},
        'model_limits': {'test-model': limits}, 'request_id': uuid.uuid4().hex})
    assert response.status_code == 200, response.text
    profile = response.json()
    tested = client.post(f"/api/ai/profiles/{profile['id']}/test", json={'request_id': uuid.uuid4().hex, 'expected_version': 1,
        'model_id': 'test-model', 'mode': 'vision_analysis', 'consent_to_send': True, 'possible_cost_confirmed': True})
    assert tested.status_code == 200, tested.text
    assert client.get(f"/api/ai/profiles/{profile['id']}/tests").json()[0]['status'] == 'ready'
    profile = client.put(f"/api/ai/profiles/{profile['id']}", json={'base_version': 1, 'changes': {'status': 'enabled'}}).json()
    revision = client.get(f'/api/projects/{pid}').json()['project']['project_revision']
    bound = client.put(f'/api/projects/{pid}/drawing/provider-binding', json={'profile_id': profile['id'],
        'config_revision': profile['current_version'], 'model_id': 'test-model', 'purpose': 'vision', 'expected_project_revision': revision})
    assert bound.status_code == 200, bound.text
    return profile


def payload(vid, **extra):
    return {'asset_version_id': vid, 'request_id': uuid.uuid4().hex, 'check_types': ['readability', 'semantic'], **extra}


def test_two_images_full_framing_output_margin_and_exact_boundary():
    admission = {'model': 'test-model', 'model_limits': {'test-model': bounds()}}
    result = vision_capacity.admit(admission, [png(), png('blue')], ['readability', 'semantic'])
    assert result['image_count'] == 2 and result['estimated_image_tokens'] == 4000
    assert result['estimated_text_and_framing_tokens'] > len(vision_capacity.instruction(['readability', 'semantic']))
    assert result['reserved_output_tokens'] == 1600 and result['safety_margin_tokens'] >= 128
    total = result['total_with_output_and_margin']
    admission['model_limits']['test-model']['input_tokens'] = total
    assert vision_capacity.admit(admission, [png(), png('blue')], ['readability', 'semantic'])['total_with_output_and_margin'] == total
    admission['model_limits']['test-model']['input_tokens'] -= 1
    with pytest.raises(ProjectServiceError, match='完整视觉请求') as error:
        vision_capacity.admit(admission, [png(), png('blue')], ['readability', 'semantic'])
    assert error.value.code == 'vision_capacity_exceeded'


@pytest.mark.parametrize('change,code', [
    ({'vision': None}, 'invalid_vision_limits'),
    ({'vision': {'tokens_per_image': True, 'max_width': 1600, 'max_height': 1600, 'source': 'fixture'}}, 'invalid_vision_limits'),
    ({'vision': {'tokens_per_image': 100, 'max_width': 1601, 'max_height': 1600, 'source': 'fixture'}}, 'invalid_vision_limits'),
    ({'vision': {'tokens_per_image': 100, 'max_width': 1600, 'max_height': 1600, 'source': ' '}}, 'vision_capacity_source_required'),
])
def test_vision_metadata_requires_valid_bounds_and_specific_source(change, code):
    with pytest.raises(ProjectServiceError) as error:
        provider_runtime.limits({'test-model': bounds(**change)}, ['test-model'])
    assert error.value.code == code


@pytest.mark.parametrize('change,code', [
    ({'vision': None}, 'vision_capacity_unknown'),
    ({'input_tokens': 2000}, 'vision_capacity_exceeded'),
    ({'output_tokens': 1599}, 'vision_capacity_exceeded'),
    ({'vision': {'tokens_per_image': 1000, 'max_width': 29, 'max_height': 20, 'source': 'dimension fixture'}}, 'vision_dimensions_exceeded'),
])
def test_preview_and_business_block_before_any_http_or_persistence(client, change, code):
    pid = project(client)
    vid = upload(client, pid).json()['version']['id']
    limits = bounds(**change)
    if limits['vision'] is None:
        del limits['vision']
    with local_provider() as (url, calls):
        enabled(client, pid, url, limits)
        calls.clear()
        data = payload(vid)
        preview = client.post(f'/api/projects/{pid}/visual-checks/preview', json=data)
        assert preview.status_code == 200, preview.text
        assert not preview.json()['generation_allowed'] and preview.json()['error_code'] == code
        assert preview.json()['external_calls'] == 0 and preview.json()['repair']['purpose'] == 'vision'
        denied = client.post(f'/api/projects/{pid}/visual-checks', json={**data, 'cloud': True, 'external_consent': True})
        assert denied.status_code == 422 and denied.json()['detail']['code'] == code
        assert calls == []
        conn = app.db()
        assert conn.execute('SELECT COUNT(*) FROM visual_check_runs WHERE project_id=?', (pid,)).fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM ai_provider_audit WHERE project_id=?', (pid,)).fetchone()[0] == 0
        conn.close()


def test_real_http_two_image_crop_preview_consent_capacity_and_immutable_body(client):
    pid = project(client)
    target = upload(client, pid).json()['version']['id']
    source = upload(client, pid, png('blue'), request_id='source').json()['version']['id']
    data = payload(target, source_asset_version=source, region={'x': 0, 'y': 0, 'width': .5, 'height': .5})
    before = client.get(f'/api/projects/{pid}').json()['files']
    with local_provider() as (url, calls):
        enabled(client, pid, url, bounds())
        calls.clear()
        preview = client.post(f'/api/projects/{pid}/visual-checks/preview', json=data).json()
        c = preview['capacity']
        assert preview['generation_allowed'] and c['image_count'] == 2 and calls == []
        assert [(i['width'], i['height']) for i in c['images']] == [(30, 20), (15, 10)]
        denied = client.post(f'/api/projects/{pid}/visual-checks', json={**data, 'cloud': True})
        assert denied.json()['detail']['code'] == 'external_consent_required' and not calls
        sent = client.post(f'/api/projects/{pid}/visual-checks', json={**data, 'cloud': True,
            'external_consent': True, 'capacity_preview_hash': preview['preview_hash']})
        assert sent.status_code == 200, sent.text
        run = client.get(f"/api/projects/{pid}/visual-checks/{sent.json()['id']}").json()
        assert run['status'] == 'completed' and run['capacity'] == c and len(calls) == 1
        parts = calls[0]['body']['messages'][0]['content']
        assert len(parts) == 3 and all(p['image_url']['detail'] == 'high' for p in parts[1:])
        assert calls[0]['body']['max_tokens'] == 1600
        assert client.get(f'/api/projects/{pid}').json()['files'] == before
        # Reusing the identical authorized request cannot trigger a second call.
        replay = client.post(f'/api/projects/{pid}/visual-checks', json={**data, 'cloud': True,
            'external_consent': True, 'capacity_preview_hash': preview['preview_hash']})
        assert replay.json()['idempotent_replay'] and len(calls) == 1


def test_changed_preview_hash_and_legacy_queue_cannot_bypass_capacity(client):
    pid = project(client)
    vid = upload(client, pid).json()['version']['id']
    with local_provider() as (url, calls):
        enabled(client, pid, url, bounds())
        calls.clear()
        data = payload(vid, cloud=True, external_consent=True)
        denied = client.post(f'/api/projects/{pid}/visual-checks', json={**data, 'capacity_preview_hash': '0' * 64})
        assert denied.status_code == 409 and denied.json()['detail']['code'] == 'vision_preview_stale'
        conn = app.db()
        with conn:
            run = visual_evidence.create(conn, pid, VisualCheckIn(**data).model_dump(exclude={'temporary_key'}), storage_root=app.PROJECTS_DIR)
            gate = json.loads(conn.execute('SELECT gate_json FROM visual_check_runs WHERE id=?', (run['id'],)).fetchone()[0])
            # Simulate a pre-migration queue with no image-bound snapshot.
            gate.pop('model_limits')
            conn.execute('UPDATE visual_check_runs SET gate_json=? WHERE id=?', (json.dumps(gate), run['id']))
        conn.close()
        visual_evidence.execute(app.db, app.PROJECTS_DIR, lambda _: None, pid, run['id'])
        read = client.get(f"/api/projects/{pid}/visual-checks/{run['id']}").json()
        assert read['status'] == 'failed' and read['error_code'] == 'vision_capacity_unknown' and calls == []
        report = client.get(f"/api/projects/{pid}/visual-reports/{read['report_id']}").json()
        assert any('补齐视觉模型容量' in f['message'] for f in report['findings'])


def test_direct_adapter_unknown_or_oversize_cannot_send_http():
    with local_provider() as (url, calls):
        admission = {'model': 'test-model', 'base_url': url + '/v1', 'auth_mode': 'none'}
        with pytest.raises(ProjectServiceError) as error:
            analyze(admission, [png()], ['readability'])
        assert error.value.code == 'vision_capacity_unknown' and calls == []
        admission['model_limits'] = {'test-model': bounds(input_tokens=1000)}
        with pytest.raises(ProjectServiceError) as error:
            analyze(admission, [png(), png()], ['readability'])
        assert error.value.code == 'vision_capacity_exceeded' and calls == []
