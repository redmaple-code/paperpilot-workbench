"""Build the project-owned toolchain and optionally configure the local launcher."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="paperpilot-tex:local")
    parser.add_argument("--base-image", default="ubuntu:22.04")
    parser.add_argument("--apt-mirror", default="archive.ubuntu.com", help="Ubuntu mirror hostname; signed packages remain verified")
    parser.add_argument("--configure", action="store_true", help="Write only the workspace launcher's non-secret image setting")
    args = parser.parse_args()
    for value in (args.image, args.base_image):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._/:@-]{0,240}", value):
            parser.error("Image references must be lowercase Docker names, with an optional tag/digest.")
    if not re.fullmatch(r"[a-z0-9.-]+", args.apt_mirror):
        parser.error("APT mirror must be a hostname.")
    result = subprocess.run(["docker", "build", "--pull=false", "--build-arg", "BASE_IMAGE="+args.base_image,
        "--build-arg", "APT_MIRROR="+args.apt_mirror,
        "--tag", args.image, str(ROOT / "deploy/tex-sandbox")], check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    subprocess.run(["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", args.image, "pdflatex", "--version"], check=True)
    if args.configure:
        config = ROOT / "data/tex-sandbox.json"
        config.parent.mkdir(exist_ok=True)
        pending = config.with_suffix(".json.tmp")
        pending.write_text(json.dumps({"image": args.image}, indent=2), encoding="utf-8")
        pending.replace(config)
        print("Configured workspace launcher. Start with: python start.py")


if __name__ == "__main__":
    main()
