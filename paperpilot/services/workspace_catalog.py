"""Stable author-facing entries shared by the directory and the agent."""
from paperpilot.services.agent_tools import Tool
import uuid


def entry(key, title, group, route, requirements, description, *, aliases='', selector='', module='paper', agent=False):
    return dict(id=key, title=title, group=group, route=route, requirement_ids=requirements.split(),
                description=description, aliases=aliases, selector=selector, module=module,
                execution='Agent 可执行' if agent else '手动工具')


ENTRIES = [
    entry('research', '领域研究与创新', '研究与资料', 'research', 'REQ-021', '讨论研究问题、创新候选与证据，查看研究知识图。', module='research', agent=True),
    entry('innovations', '论文创新点', '研究与资料', 'innovations', 'REQ-027', '审阅、确认和修订本文的创新点，查看来源与下游引用。', aliases='主创新 副创新 研究贡献', module='research'),
    entry('search', '发现论文', '研究与资料', 'knowledge/discover', 'REQ-019', '检索真实学术来源，核对并保存候选。', aliases='学术搜索 推荐', selector='#academicSearchCard', module='knowledge', agent=True),
    entry('import', '外部论文导入', '研究与资料', 'knowledge/references', 'REQ-018', '获取公开全文、查看预览并确认入库。', aliases='OA PDF', selector='#referenceLibraryCard', module='knowledge'),
    entry('manuscript-import', '已有论文导入', '研究与资料', 'manuscript-import', 'REQ-025', '上传自己的稿件，核对拆分结果并采纳到论文工作区。', aliases='整篇导入 PDF DOCX LaTeX ZIP 批次', module='writing'),
    entry('references', '参考文献', '研究与资料', 'knowledge/references', 'REQ-005', '管理引用条目、来源和待确认文献。', selector='#refList', module='knowledge', agent=True),
    entry('corpus', '语料库', '研究与资料', 'knowledge/corpus', 'REQ-009', '查看授权材料、原文检索、来源和版本。', selector='#corpusList', module='knowledge', agent=True),
    entry('terms', '术语库', '研究与资料', 'knowledge/terms', 'REQ-002', '维护定义、别名和领域术语。', selector='#termLibrary', module='evidence', agent=True),
    entry('domain', '领域资料与复用', '研究与资料', 'knowledge/shared', 'REQ-016 REQ-017', '查看领域资料，跨论文复用术语、语料和文献。', aliases='多领域 跨论文复用 复制资源 复制术语 复制语料 复制文献', selector='#copyResource', module='knowledge'),
    entry('manuscript', '论文正文', '稿件与结构', 'write', 'REQ-001 REQ-015', '编辑当前文件，引用选区与 Agent 协作。', aliases='自动论文写作 编辑器', selector='#editor', module='writing', agent=True),
    entry('outline', '章节大纲', '稿件与结构', 'outline/nodes', 'REQ-003', '讨论章节目标，审阅结构修改。', aliases='目录 写作大纲', module='outline', agent=True),
    entry('logic', '论证图', '稿件与结构', 'knowledge/graph', 'REQ-004', '查看主张、证据与论证依赖。', aliases='逻辑链路', selector='#logicGraphCard', module='logic', agent=True),
    entry('companion', '自动伴写', '稿件与结构', 'write', 'REQ-006', '围绕当前段落与材料提出写作候选。', module='writing', agent=True),
    entry('follow', '跟写', '稿件与结构', 'write', 'REQ-007', '选中示例，延续表达方式生成候选。', module='writing', agent=True),
    entry('continue', '续写', '稿件与结构', 'write', 'REQ-008', '基于现有正文、大纲和目标继续写作。', module='writing', agent=True),
    entry('modify', '指定部分修改', '稿件与结构', 'write', 'REQ-010', '选中目标段落，提出修改要求并核对候选。', aliases='定向修改 润色', module='writing', agent=True),
    entry('revision', '人工修订模式', '稿件与结构', 'write', 'REQ-015', '进入或退出人工修订，管理检查点与共享草稿。', selector='#workspaceRevisionControls', module='writing'),
    entry('results', '结果数据', '图像与证据', 'visual/results', 'REQ-026', '保存原始数据与处理版本，按版本生成论文图表。', aliases='实验 仿真 CSV TSV 散点图 折线图', module='visual'),
    entry('images', '图像库与版本', '图像与证据', 'visual/library', 'REQ-022', '导入、替换、选择图像版本并插入正文。', selector='#openVisualImport', module='visual'),
    entry('draw', '图像生成与编辑', '图像与证据', 'visual/drawing', 'REQ-023', '选择本地绘图或图像模型，审阅生成候选。', aliases='图片绘制 配图', selector='#workspaceDrawingRequest', module='visual'),
    entry('vision', '视觉证据与图文检查', '图像与证据', 'visual/review', 'REQ-024', '识别图像内容，核对来源与图文一致性。', module='visual'),
    entry('compare', '文献支撑与表达对照', '检查与交付', 'knowledge/references', 'REQ-013', '对照正文主张、来源原文和表达。', selector='#citationReviewPanel', module='delivery'),
    entry('citation', '引用检查', '检查与交付', 'knowledge/references', 'REQ-011 REQ-013', '检查当前正文引用及参考文献。', selector='#compare', module='delivery'),
    entry('compile', 'LaTeX 编译', '检查与交付', 'history/builds', 'REQ-011', '选择构建用途，编译并查看日志。', selector='#compile', module='delivery'),
    entry('pdf', 'PDF 阅读与预览', '检查与交付', 'preview', 'REQ-011', '阅读与源码版本对应的 PDF。', module='reading'),
    entry('release', '发布与导出', '检查与交付', 'planning/progress', 'REQ-005 REQ-021', '查看发布条件、检查结果与交付版本。', selector='#projectLifecycleCard', module='delivery'),
    entry('progress', '论文进展', '进展与待办', 'planning/progress', 'REQ-021', '按需查看里程碑、依赖及下一步。', module='resources', agent=True),
    entry('resources', '资源待办', '进展与待办', 'planning/resources', 'REQ-021', '管理材料缺口、工作量与待办状态。', selector='#workflowWorkItems', module='resources', agent=True),
    entry('versions', '内容版本与恢复', '历史与恢复', 'history/content', 'REQ-001 REQ-015', '比较和恢复已保存内容版本。', module='writing'),
    entry('revisions', '修订记录', '历史与恢复', 'history/revisions', 'REQ-015', '查看人工修订记录与恢复副本。', module='writing'),
    entry('recovery', '人工修订恢复副本', '历史与恢复', 'write', 'REQ-015', '比较、恢复和管理人工修订草稿副本。', selector='#revisionCopiesRefresh', module='writing'),
    entry('tasks', 'AI 任务与恢复', '历史与恢复', 'history/ai', 'REQ-001 REQ-008', '查看执行状态、失败原因及恢复入口。'),
    entry('paper', '论文与文风配置', '设置', 'settings/paper', 'REQ-017', '设置论文偏好、领域及文风。'),
    entry('model', '文本与图像模型', '设置', 'settings/models', 'REQ-014', '登记提供方、测试能力并绑定用途。', aliases='API 提供方 模型测试'),
    entry('style', '文风学习与经验', '设置', 'settings/learning', 'REQ-020', '查看有来源的文风与经验候选。'),
    entry('agent', '发送范围与预算', '设置', 'agent-settings', 'REQ-001 REQ-014', '核对当前对话的接收方、材料范围和预算。'),
    entry('papers', '论文库', '研究与资料', 'paper-library', 'REQ-005 REQ-016', '查找、切换和管理论文。'),
]


def catalog(query=''):
    query = query.strip().casefold()
    return [item.copy() for item in ENTRIES if not query or query in
            ' '.join([item['title'], item['group'], item['description'], item['aliases'], *item['requirement_ids']]).casefold()]


def open_artifact(args, context):
    item=next(item for item in ENTRIES if item['id']==args['id'])
    card=dict(item,kind='artifact',entry_id=item['id'],id=uuid.uuid4().hex,open_requested=True,
              project_id=context['project_id'],conversation_id=context.get('conversation_id',''),node_id=args.get('node_id',''),
              target=context.get('target') or {})
    return {'cards':[card]}


def tools():
    from paperpilot.services.result_conversations import group_library_tool, prepare_tool
    from paperpilot.services.agent_tools import WRITE_CANDIDATE

    return [Tool('paper_tools', '查找论文工具及固定入口。返回实际执行方式，使用返回的route打开成果。',
                 {'type': 'object', 'properties': {'query': {'type': 'string', 'maxLength': 120}}},
                 lambda args, context: {'items': catalog(args.get('query', ''))}, result_cap=24000),
            Tool('open_artifact', '向作者提供本论文成果入口。仅作者要求查看时使用，必须引用目录中的id。',
                 {'type': 'object', 'properties': {'id': {'type': 'string', 'enum': [i['id'] for i in ENTRIES]}, 'node_id':{'type':'string','maxLength':120}}, 'required': ['id']},
                 open_artifact),
            Tool('prepare_result', '按作者要求在当前论文查找、整理或绘制本地实验数据。把任务改述为含明确算例、网格、来源、数据集、列和范围的中文指令。返回可在界面审阅的结果卡，文件和数值在本地读取，写入由作者确认。',
                 {'type':'object','properties':{'instruction':{'type':'string','minLength':1,'maxLength':2000}},
                  'required':['instruction'],'additionalProperties':False}, prepare_tool,
                 side_effect=WRITE_CANDIDATE, max_calls_per_run=3, retries=0),
            Tool('group_result_library', '根据作者要求分析结果库的匿名结构与版本关系，建立可手工调整的自动分组。',
                 {'type':'object','properties':{},'additionalProperties':False}, group_library_tool,
                 side_effect=WRITE_CANDIDATE, timeout_ms=90000, max_calls_per_run=1, retries=0)]
