"""Isolated UI fixture server. Never uses private storage or real model credentials."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--storage", required=True)
parser.add_argument("--port", type=int, default=8896)
args = parser.parse_args()
storage = Path(args.storage).resolve()
storage.mkdir(parents=True, exist_ok=True)
os.environ["PWA_DATA_DIR"] = str(storage)
os.environ["PWA_HOST_BUDGET_DB"] = str(storage/"host-budget.sqlite3")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
from tests.provider_fixtures import install_default_provider_fixture
from paperpilot.services import academic_search, research_stage_policies
app.init_db()
install_default_provider_fixture()


def model_fixture(*args, **kwargs):
    text = " ".join(map(str,args)) + str(kwargs)
    match = re.search(r'STAGE-0[1-5]', text)
    stage = match.group() if match else "STAGE-01"
    fields = research_stage_policies.policy(stage)["fields"]
    card = {"title":"隔离界面测试候选", "idea":"验证不同边界条件下的证据一致性", "payload":{key:"界面测试草稿："+label for key,label in fields.items()},
            "limitations":"合成界面测试数据，未执行真实模型或实验。", "search_queries":["reproducible research"]}
    return {"ok":True,"content":json.dumps({"cards":[card]},ensure_ascii=False),"usage":{"prompt_tokens":100,"completion_tokens":100}}


class SearchFixture:
    name = "fixture"
    def search(self, query, *_):
        return {"results":[{"id":"https://example.org/one","title":"合成检索结果一","publication_year":2025,"doi":"10.1234/ui-one",
                            "open_access":{"is_oa":True},"best_oa_location":{"is_oa":True,"pdf_url":"https://example.org/one.pdf"}},
                           {"id":"https://example.org/two","title":"合成检索结果二","publication_year":2024}],"next_cursor":None}


app.call_ai = model_fixture
academic_search.provider_from_env = lambda: SearchFixture()
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app.app,host="127.0.0.1",port=args.port,log_level="warning")
