import json
import requests

BASE = 'http://127.0.0.1:8765'

def check(label, fn):
    try:
        result = fn()
        print(f'{label}: PASS {json.dumps(result, ensure_ascii=False)}')
    except Exception as exc:
        print(f'{label}: FAIL {type(exc).__name__}: {exc}')


state = {}

def group1():
    health = requests.get(BASE + '/api/health', timeout=10).json()
    config = requests.get(BASE + '/api/config', timeout=10).json()
    assert health['ok'] and config['configured'] and 'api_key' not in config
    state['health'] = health
    return {'health_ok': health['ok'], 'ai_configured': config['configured'], 'provider': config['provider']}

def group2():
    p = requests.post(BASE + '/api/projects', json={'name':'集成测试论文','domain':'计算材料','style':'Nature'}, timeout=10).json()
    state['pid'] = p['project']['id']
    assert {x['path'] for x in p['files']} == {'main.tex','references.bib'}
    save = requests.put(f"{BASE}/api/projects/{state['pid']}/files", json={'path':'main.tex','content':'\\documentclass{article}\n\\begin{document}Smoke\\end{document}'}, timeout=10).json()
    outline = requests.put(f"{BASE}/api/projects/{state['pid']}/outline", json={'content':'1. Introduction\n2. Results'}, timeout=10).json()
    assert save['ok'] and outline['ok']
    return {'project_id': state['pid'], 'files': [x['path'] for x in p['files']], 'save_ok': save['ok'], 'outline_ok': outline['ok']}

def group3():
    pid = state['pid']
    a = requests.post(BASE + '/api/convert', json={'direction':'markdown_to_latex','source':'# Title\n\n**bold**'}, timeout=10).json()['content']
    b = requests.post(BASE + '/api/convert', json={'direction':'latex_to_markdown','source':a}, timeout=10).json()['content']
    term = requests.post(f'{BASE}/api/projects/{pid}/terms', json={'term':'graphene','definition':'二维材料','aliases':'碳材料'}, timeout=10).json()
    copied = requests.post(f'{BASE}/api/projects/{pid}/copy-resource', json={'source_project_id':pid,'resource_type':'term','resource_id':term['id']}, timeout=10).json()
    requests.post(f'{BASE}/api/projects/{pid}/refs', json={'cite_key':'smith2024','title':'A study'}, timeout=10)
    requests.put(f'{BASE}/api/projects/{pid}/files', json={'path':'main.tex','content':'Text \\cite{smith2024,missing2020}.'}, timeout=10)
    compare = requests.post(f'{BASE}/api/projects/{pid}/compare', timeout=10).json()
    project = requests.get(f'{BASE}/api/projects/{pid}', timeout=10).json()
    assert '\\section{Title}' in a and '# Title' in b and 'missing2020' in compare['missing'] and copied['ok']
    assert any(x.get('source_resource_id') == term['id'] for x in project['terms'])
    return {'markdown_latex_roundtrip': True, 'missing_citation_detected': compare['missing'], 'resource_copy_source_tracked': True}

def group4():
    pid = state['pid']
    requests.put(f'{BASE}/api/projects/{pid}/files', json={'path':'main.tex','content':'\\documentclass{article}\n\\begin{document}Hello PDF\\end{document}'}, timeout=10)
    result = requests.post(f'{BASE}/api/projects/{pid}/compile', timeout=70).json()
    if result.get('ok'):
        pdf = requests.get(f'{BASE}/api/projects/{pid}/pdf', timeout=10)
        assert pdf.status_code == 200 and pdf.headers.get('content-type','').startswith('application/pdf')
    return {'compile_ok': result.get('ok'), 'pdf_available': bool(result.get('pdf_url')), 'log_tail_chars': len(result.get('log',''))}

def group5():
    pid = state['pid']
    companion = requests.post(f'{BASE}/api/projects/{pid}/ai', json={'action':'companion','prompt':'给出下一句候选','content':'研究二维材料的稳定性。'}, timeout=100).json()
    follow = requests.post(f'{BASE}/api/projects/{pid}/ai', json={'action':'follow','prompt':'沿用户句式继续','content':'实验结果表明'}, timeout=100).json()
    assert companion.get('ok') and follow.get('ok')
    return {'ai_request_ok': True, 'model': companion.get('model'), 'actions': ['companion','follow'], 'response_chars': len(companion.get('content','')) + len(follow.get('content',''))}

check('GROUP-1 health/config', group1)
check('GROUP-2 project/files/outline', group2)
check('GROUP-3 conversion/knowledge/compare', group3)
check('GROUP-4 latex/pdf', group4)
check('GROUP-5 deepseek-ai', group5)
