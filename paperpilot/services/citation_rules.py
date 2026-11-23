"""Explicit evidence provenance. Similar wording and author identity never establish a study relation."""
import copy
import hashlib
import json

from paperpilot.services import citation_support, settings
from paperpilot.services.project_errors import ProjectServiceError

RULE_VERSION='citation-review-v3'


def compact(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value):return hashlib.sha256(compact(value).encode()).hexdigest()


def initialize_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS citation_source_profiles(
        project_id TEXT NOT NULL, source_id TEXT NOT NULL, version INTEGER NOT NULL,
        metadata_json TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(project_id,source_id))''')


def profiles(conn,project_id):
    return {r['source_id']:{'version':r['version'],**json.loads(r['metadata_json'])} for r in conn.execute('SELECT * FROM citation_source_profiles WHERE project_id=?',(project_id,))}


def update_profile(conn,project_id,source_id,expected_version,metadata):
    if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
    available={s['source_id']:s for s in citation_support.analyze(conn,project_id,content='')['source_pool']}
    if source_id not in available:
        raise ProjectServiceError(404,'citation_source_missing','来源不在当前论文的文献快照内。')
    old=conn.execute('SELECT version FROM citation_source_profiles WHERE project_id=? AND source_id=?',(project_id,source_id)).fetchone()
    version=old['version'] if old else 0
    if version!=expected_version:
        raise ProjectServiceError(409,'source_profile_conflict','来源核验记录已更新，请刷新后比较，当前输入保留。')
    if metadata.get('origin_source_id') and (metadata['origin_source_id'] not in available or metadata['origin_source_id']==source_id):
        raise ProjectServiceError(422,'source_origin_invalid','实际原文须为当前论文内另一个明确来源；未知时留空。')
    metadata={**metadata,'source_signature':digest(available[source_id])}
    conn.execute('''INSERT INTO citation_source_profiles VALUES(?,?,?,?,?)
        ON CONFLICT(project_id,source_id) DO UPDATE SET version=excluded.version,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at''',
        (project_id,source_id,version+1,compact(metadata),citation_support._now()))
    return {'source_id':source_id,'version':version+1,**metadata}


def identity(conn,project_id):
    from paperpilot.services.context import material_fingerprint
    return {'materials':material_fingerprint(conn,project_id),'configuration':settings.read(conn,project_id),
            'source_profiles':profiles(conn,project_id),'rule_version':RULE_VERSION}


def enrich(conn,project_id,report,paper_language='auto',claim_types=None):
    report=copy.deepcopy(report)
    metadata=profiles(conn,project_id)
    report['review_protocol']=RULE_VERSION
    report['context']['paper_language']=paper_language
    report['context']['term_priority']='explicit_paper_terms_first'
    report['policy']['no_author_identity_exclusion']=True
    by_key={}
    groups={};unknown=[]
    for source in report['source_pool']:
        profile=metadata.get(source['source_id'],{})
        if profile.get('source_signature')!=digest(source):
            profile={'version':profile.get('version',0)}
        source['provenance']={
            'parse_reliability':profile.get('parse_reliability','unverified'),
            'source_role':profile.get('source_role','unknown'),
            'origin_source_id':profile.get('origin_source_id',''),
            'derived_from_current':profile.get('derived_from_current',False),
            'evidence_kind':profile.get('evidence_kind','text'),
            'visual_asset_version_id':profile.get('visual_asset_version_id',''),
            'study_group':profile.get('study_group',''),
            'profile_version':profile.get('version',0)}
        provenance=source['provenance']
        group=provenance['study_group']
        if group:groups.setdefault(group,[]).append(source['source_id'])
        else:unknown.append(source['source_id'])
        reasons=[]
        if provenance['derived_from_current']:
            source['evidence']=[];source['evidence_text']=''
            reasons.append('当前论文或派生副本不能循环自证；同作者既有正式论文并不因此排除')
        if provenance['parse_reliability']!='verified':reasons.append('解析内容尚未可靠核对，不能作为已核验原文支持')
        if provenance['source_role'] in {'secondary','note'}:reasons.append('二手转引/笔记只指向实际原文来源；未核验原文不得伪造其引句页码')
        if provenance['evidence_kind'] in {'table','figure','mixed'}:reasons.append('图表数值须核对原图页/区域，图题或OCR不足以证明数值')
        if reasons:source['full_text_presence']='needs_human_review'
        source['evidence_limits']=reasons
        by_key[source['cite_key']]=source
    for claim in report['claims']:
        override=(claim_types or {}).get(claim['id'])
        if override:
            claim['inferred_claim_type']=claim['claim_type'];claim['claim_type']=override;claim['claim_type_source']='explicit_user'
        claim['support_required']=claim['claim_type']!='original_result'
        claim['interpretation_requires_separate_review']=claim['claim_type']=='original_result'
        claim['condition_results']=[{'index':i,'condition':c,'status':'unknown','reason':'尚未逐条件核验'} for i,c in enumerate(claim['conditions'])]
        claim['language_review']={'paper_language':paper_language,'auxiliary_translation':'','translation_is_auxiliary':True,'concept_differences':[],'status':'not_checked'}
        for support in claim.get('supports',[]):
            source=by_key.get(support['cite_key'])
            if source:
                support['provenance']=source['provenance'];support['evidence_limits']=source['evidence_limits']
                if source['provenance']['derived_from_current']:
                    support['evidence']=[];support['evidence_text']='';support['source_identity']=''
                    support['support_status']='not_verifiable'
    report['study_relations']={'known_groups':groups,'known_group_count':len(groups),'unknown_relation_sources':unknown,
        'policy':'已知同研究合并；未知关系保持未知，不凭相似度或作者身份认定独立或相同'}
    return report


def enforce_limits(report):
    """Applied after model parsing; no exact quote may bypass source provenance."""
    by_id={s['source_id']:s for s in report.get('source_pool',[])}
    for claim in report.get('claims',[]):
        review=claim.get('semantic_review',{})
        for finding in review.get('sources',[]):
            source=by_id.get(finding.get('source_id'))
            if source and source.get('evidence_limits'):
                finding['full_text']=False
                finding['relation']='insufficient_evidence'
                finding['rationale']='；'.join(source['evidence_limits'])
        for support in claim.get('supports',[]):
            if support.get('evidence_limits'):
                support['support_status']='not_verifiable';support['confidence']='low'
            if claim.get('condition_results') and any(c['status']!='supported' for c in claim['condition_results']) and support.get('support_status')=='supported':
                support['support_status']='partial'
        claim['condition_coverage']={'total':len(claim.get('condition_results',[])),
            'reviewed':sum(c['status']!='unknown' for c in claim.get('condition_results',[]))}
    from paperpilot.services.citation_semantic import _refresh_summary
    _refresh_summary(report)
    return report
