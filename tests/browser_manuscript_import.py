"""Run the real author-facing import flow against an isolated PaperPilot server."""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright


@contextmanager
def isolated_server():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
    url=f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory(prefix='paperpilot-ui-manuscript-') as folder:
        root=Path(folder)
        environ={**os.environ,'PWA_DATA_DIR':str(root),'PWA_DB_PATH':str(root/'db'),
                 'PWA_PROJECTS_DIR':str(root/'projects'),'PWA_HOST_BUDGET_DB':str(root/'budget.db')}
        server=subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port',str(port)],
                                env=environ,cwd=Path(__file__).resolve().parents[1],
                                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            with requests.Session() as session:
                session.trust_env=False
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError('Isolated server exited before starting.')
                    try:
                        if session.get(url+'/api/health',timeout=1).ok:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(.2)
                else:
                    raise RuntimeError('Isolated server did not become ready.')
            yield url
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=10)


def main():
    options=argparse.ArgumentParser()
    options.add_argument('--base-url')
    options.add_argument('--browser',type=Path)
    args=options.parse_args()
    scope=nullcontext(args.base_url) if args.base_url else isolated_server()
    with scope as url, sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True,executable_path=args.browser,args=['--no-proxy-server'])
        page=browser.new_page(viewport={'width':1250,'height':880})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(url,wait_until='domcontentloaded')
        page.wait_for_load_state('networkidle')
        if page.locator('#newDialog[open]').count():
            page.locator('#newDialog').get_by_role('button',name='导入已有论文').click()
        else:
            page.locator('#openManuscriptImport').click()
        dialog=page.get_by_role('dialog',name='导入已有论文')
        dialog.get_by_label('导入目标').select_option('')
        dialog.get_by_text('新论文名称',exact=True).locator('input').fill('Browser manuscript fixture')
        dialog.get_by_role('button',name='选择稿件和资料').click()
        dialog.get_by_label('选择稿件和辅助材料').set_input_files({
            'name':'browser-draft.md','mimeType':'text/markdown',
            'buffer':b'# Abstract\nA local manuscript can be resumed in the editor.\n\n# Methods\nA verified fixture validates the import.\n'})
        dialog.get_by_role('button',name='暂存并解析文件').click()
        expect(dialog.get_by_role('status')).to_contain_text('已暂存')
        dialog.get_by_text('来源文件与解析 · 1 份').click()
        dialog.get_by_text('正文或笔记 · browser-draft.md').click()
        expect(dialog.get_by_role('link',name='下载原件')).to_be_visible()
        dialog.get_by_role('button',name='预览本地拆分').click()
        expect(dialog.get_by_role('button',name='论文正文 · 3 项，0 项已采纳')).to_be_visible()
        dialog.get_by_role('button',name='选择可新增的有来源条目').click()
        dialog.get_by_role('button',name='采纳所选').click()
        expect(dialog.locator('#manuscriptImportStatus')).to_contain_text('0 组失败')
        expect(dialog.get_by_role('button',name='论文正文 · 3 项，3 项已采纳')).to_be_visible()
        dialog.get_by_role('button',name='打开论文工作区').click()
        expect(page.locator('#projectName')).to_have_value('Browser manuscript fixture')
        page.evaluate("window.paperpilot.openWorkspace('write')")
        page.get_by_role('button',name='导入结果',exact=True).click()
        expect(dialog.get_by_role('button',name='论文正文 · 3 项，3 项已采纳')).to_be_visible()
        dialog.get_by_role('button',name='关闭',exact=True).click()
        expect(dialog).to_be_hidden()
        page.locator('#closePaperArtifact').click()
        project_id=page.evaluate('window.paperpilot.current.project.id')
        saved=page.request.get(f'{url}/api/projects/{project_id}').json()
        assert any('verified fixture' in file['content'] for file in saved['files'])
        page.locator('#openManuscriptImport').click()
        dialog.get_by_label('导入目标').select_option(project_id)
        dialog.get_by_role('button',name='选择稿件和资料').click()
        dialog.get_by_label('选择稿件和辅助材料').set_input_files({
            'name':'additional-data.csv','mimeType':'text/csv','buffer':b'run,value\n1,7\n'})
        dialog.get_by_role('button',name='暂存并解析文件').click()
        expect(dialog.get_by_role('status')).to_contain_text('已暂存')
        dialog.get_by_role('button',name='预览本地拆分').click()
        dialog.get_by_role('button',name='选择可新增的有来源条目').click()
        dialog.get_by_role('button',name='采纳所选').click()
        expect(dialog.locator('#manuscriptImportStatus')).to_contain_text('0 组失败')
        merged=page.request.get(f'{url}/api/projects/{project_id}').json()
        assert any('run,value' in corpus['content'] for corpus in merged['corpus'])
        assert not errors,errors
        browser.close()
    print('Browser manuscript import: new and existing targets, original preview, adoption, editor handoff passed')


if __name__=='__main__':
    main()
