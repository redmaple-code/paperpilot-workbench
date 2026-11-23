"""Start this workspace with its local non-secret TeX image configuration."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from dotenv import load_dotenv
import uvicorn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    config = Path(os.environ.get("PWA_DATA_DIR", str(root / "data"))) / "tex-sandbox.json"
    if not os.environ.get("PWA_TEX_SANDBOX_IMAGE") and config.is_file():
        image = json.loads(config.read_text(encoding="utf-8")).get("image", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._/:@-]{0,240}", image):
            parser.error("本地 TeX 镜像配置无效，请重新运行 scripts/setup_tex_sandbox.py --configure。")
        os.environ["PWA_TEX_SANDBOX_IMAGE"] = image
    # Direct ASGI imports continue to require explicit environment configuration.
    uvicorn.run("app:app", host=args.host, port=args.port, app_dir=str(root))


if __name__ == "__main__":
    main()
