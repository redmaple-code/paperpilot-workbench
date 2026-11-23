# PaperPilot 论文写作助手

这是一个运行在本机的论文写作 MVP：论文工程、SQLite 数据、编辑器和编译服务均在本机运行；AI 通过可切换的 OpenAI 兼容接口调用。

## 使用入口

- 论文总对话用于讨论、写作和调用论文工具。
- 模块测试版可用 `?view=module` 打开独立工具页面。
- 进度和作者验收待办见 [任务表](docs/task-tracker.xlsx)。

## 启动

1. 安装依赖：`pip install -r requirements.txt`
2. 在 `.env` 配置 `PWA_AI_BASE_URL`、`PWA_AI_API_KEY`、`PWA_AI_MODEL` 和可选的 `PWA_AI_TIMEOUT`。
3. 启动 Docker Desktop，运行 `python scripts/setup_tex_sandbox.py --configure`，自动构建中文 LaTeX、BibTeX/Biber 沙箱并保存本项目启动配置。首次构建会下载工具链；需要时可加 `--apt-mirror mirrors.aliyun.com`。也可自行准备镜像并设置 `PWA_TEX_SANDBOX_IMAGE`。编译容器禁网、只挂载临时构建目录；无可用沙箱时拒绝编译，运行时不会自动拉取镜像。
4. 启动服务：`python start.py`。已有部署也可继续用 `python -m uvicorn app:app --host 127.0.0.1 --port 8765`，此方式需自行设置 `PWA_TEX_SANDBOX_IMAGE`。
5. 打开：`http://127.0.0.1:8765/`

论文总对话使用红枫 Agent 基座，需要 Node.js 22.19 或更新版本。首次安装运行 `npm --prefix agent-base ci`。启动后可在同页模型设置中登记、测试并绑定提供方，已测试的默认模型供新建论文继承。已有论文通过模型设置选择绑定。

当前界面以论文总对话为主，专题讨论和稿件、研究图、PDF按需打开。“资料与工具”提供完整功能目录，也可用 Ctrl+K 搜索入口。

检查真实编译环境：`python scripts/verify_tex_sandbox.py`。此命令在独立临时工程中验证中英文、受管图像、BibTeX 和 Biber，不修改现有论文。
端到端验证正文编辑、资产插入、构建快照与 PDF 下载：`python tests/verify_real_tex_api.py`，同样使用隔离数据库。

使用自己的 OpenAI 兼容服务地址及模型标识。按本机环境提供 API 密钥。

## 已覆盖的 MVP 闭环

- 多篇论文隔离管理，支持论文搜索、归档/恢复；每篇论文有领域、风格、引用格式和可选模型。
- 多文件 LaTeX 工程（可在编辑器中新建/删除文件）、Markdown/LaTeX 转换、Docker 隔离编译和 PDF 预览。
- 自动写作、自动伴写、跟写、续写、选中修改、AI 大纲、术语库、语料库、参考文献和引用比对。
- 逻辑链路节点、确定性 SVG 示意图，以及跨论文手动复制术语/语料/文献；复制资源保留源项目和源资源 ID。
- 文献语义检查默认只做本地只读分析；调用外部模型前须先检查完整预览并逐次授权。支持判定必须引用当前授权来源中的真实片段；词汇相似度只用于检索提示。
- 每篇论文可单独指定模型和 OpenAI 兼容 Base URL；留空时继承全局模型配置，也可将 Base URL 指向本机模型服务。

## 导入已有论文

在论文总对话或新建论文窗口选择「导入已有论文」。可以新建论文或选择已有工作区，同一批次上传主稿和辅助资料。支持 PDF（含扫描和混合页）、DOCX、Markdown、TXT、LaTeX、ZIP 工程、BibTeX、CSV 与常用图像，单个文件最多 20 MB。多个主稿需要先选正文来源。原件和解析结果暂存，扫描页可本地识别、重试或手工校对。

先看本地拆分，或确认接收方和片段后运行 AI。逐项预览正文、大纲、研究草稿、论证、术语、语料、书目和图像，再采纳到工作区。已有冲突默认保留，替换需明确选择。采纳结果可回查已写入、失败和已跳过的条目。

用 `python scripts/verify_manuscript_import.py --fixture` 验证自带合成论文，或使用 `--source` 指定一批实际文件。验证脚本在独立临时数据库中执行，并输出带来源定位的模块报告。真实模型验证读取环境变量 `PWA_AI_API_KEY`。

## 学术搜索来源

默认只查询 OpenAlex；如需多来源，可在启动服务前显式设置 `PWA_ACADEMIC_SOURCES=openalex,crossref,semanticscholar`。搜索摘要会保存来源、扩展式、页数、失败和截断提示；旧版本未保存诊断的历史会明确标注，可重新搜索。

OpenAlex 的匿名首页通常可查询，后续游标分页可能要求免费 API Key：在 [OpenAlex API 设置](https://openalex.org/settings/api)获取后，将其配置为本机 `.env` 中的 `PWA_ACADEMIC_SEARCH_API_KEY` 并重启服务，再点击“加载更多”。此前结果和游标会保留；不要将密钥粘贴到论文内容或搜索框。 如暂不配置 OpenAlex 密钥，可将 `PWA_ACADEMIC_SOURCES` 设置为 `crossref`，重启服务并重新搜索；来源与结果会有所不同。

## 公开论文获取与网络

OA 公开全文会先生成预览，确认后才写入知识库。抓取默认读取进程环境中的 `https_proxy` / `HTTPS_PROXY` 和 `http_proxy` / `HTTP_PROXY`，仅接受无用户名和密码的本机 HTTP 代理；若未配置则直连。代理必须正确执行指向已验证公网 IP 的 CONNECT，HTTPS 仍校验原始域名证书。代理不应来自不可信来源，系统不会读取 Cookie 或代理凭据。

如果本机代理未启动，请启动代理后重试；若要强制直连，可在启动服务前设置 `PAPERPILOT_OA_PROXY_MODE=direct`。恢复自动选择可设为 `auto` 或移除此变量。远程代理、SOCKS 和带凭据的代理目前不支持；网络不通时可换用公开链接。此抓取路径不处理 `NO_PROXY`，启用本机代理后，公开请求均经该代理。

## 验证

```text
pytest -q
python integration_smoke.py
```

`integration_smoke.py` 覆盖健康检查与配置、项目/文件/大纲、转换与知识库、LaTeX/PDF、真实 DeepSeek 请求共 5 组结果。

## 完整测试论文

运行下面的脚本，会在本地论文库创建一篇结构完整、可编译的多文件 LaTeX 论文：

```text
python seed_complete_paper.py
```

测试论文题目为 `A Reproducible Evaluation Framework for Interpretable Machine Learning on Tabular Data`，包含摘要、引言、相关工作、方法、实验设置、结果、讨论、结论和 BibTeX 参考文献，并预置术语库、语料库、逻辑链路、引用比对数据。论文中的数据明确标注为合成测试值，用于验证编辑、编译、PDF 预览、知识库和 AI 工作流，不代表真实科研结论。

绘图入口先给出类型建议；选择本地结构图、可编辑图形或已配置的 AI 图片工具，生成候选后再显式保存。

## 图像工作台

在论文工作区打开“图像工作台”，导入 PNG、JPEG、WebP、安全 SVG 或 PDF（最多 20 MB）；多页 PDF 需填写页码。导入后可查看预览与来源、选择历史版本、替换或归档恢复。原文件会保存为受管副本，移动本地原件不影响引用。

进入人工修订模式并保存正文后，选择图像版本，填写可选图注，再点“插入当前正文”。引用会固定为所选版本；之后替换图像不会静默改变已有正文或历史构建。跨论文复用需显式复制，复制后的版本独立演进。

绘图任务、三类输入/图像协议、本地可编辑图形、视觉报告和人工复核已实现并有技术验证。AI 图片与云视觉需要登记能力/费用并逐次授权；费率未知且有金额硬上限时拒绝调用，结果不明先对账。图像/视觉模型按用途独立绑定，保留原写作模型。视觉检查不能证明科学真实性。完整五阶段启动规划已实现并有技术验证；本实现版本由AI复核、未人工核验。文风学习、真实模型质量、其余专项和全产品验收仍需继续。
