"""Verify real TeX, bibliography and managed graphics in the restricted build adapter."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pypdf import PdfReader
from paperpilot.adapters.latex import compile_project_files
from paperpilot.services import visual_assets, visual_generation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="paperpilot-tex:local")
    args = parser.parse_args()
    os.environ["PWA_TEX_SANDBOX_IMAGE"] = args.image
    os.environ["PWA_LATEX_TIMEOUT_SECONDS"] = "120"
    local = ROOT / ".project-to-act/local/project-development/full-product-20261101"
    out = ROOT / ".project-to-act/docs/project-development/evidence/full-product-20261101"
    local.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    graph = {"nodes": [{"id": "a", "label": "Input"}, {"id": "b", "label": "Result"}], "edges": [{"source": "a", "target": "b"}]}
    svg = visual_generation.render("structured_svg", graph, {"values": visual_generation.DEFAULTS["structured_svg"]})
    graphic = visual_assets.prepare(svg, "method.svg", None)["build"]
    bib = '@article{sample,author={Example, A.},title={Reproducible bibliography verification},journal={Test Journal},year={2026}}'
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "image": args.image, "real_compiler": True, "source_is_synthetic": True, "results": []}
    with tempfile.TemporaryDirectory(prefix="real-tex-", dir=local) as temporary:
        base = Path(temporary).resolve()
        assert base.is_relative_to(local.resolve())
        for engine, bibliography in [("pdflatex", "bibtex"), ("xelatex", "biber")]:
            os.environ["PWA_LATEX_ENGINE"] = engine
            os.environ["PWA_BIB_ENGINE"] = bibliography
            if bibliography == "biber":
                main = r"\documentclass{ctexart}\usepackage{graphicx}\usepackage[backend=biber]{biblatex}\addbibresource{references.bib}\begin{document}中文编译与参考文献核验。 Real sandbox verification. \cite{sample}\includegraphics[width=0.8\linewidth]{figure.pdf}\printbibliography\end{document}"
            else:
                main = r"\documentclass{article}\usepackage{graphicx}\begin{document}Real sandbox verification. \cite{sample}\includegraphics[width=0.8\linewidth]{figure.pdf}\bibliographystyle{plain}\bibliography{references}\end{document}"
            files = [{"path": "main.tex", "content": main}, {"path": "references.bib", "content": bib}, {"path": "figure.pdf", "content_bytes": graphic}]
            result = compile_project_files(files, base / engine, lambda name: name, work_dir=base / engine / "build", job_id="real-"+engine)
            row = {"engine": engine, "bibliography": bibliography, "ok": bool(result.get("ok")), "code": result.get("code"), "log": result.get("log", "")}
            if result.get("ok"):
                pdf_path = base / engine / "build/main.pdf"
                reader = PdfReader(pdf_path)
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                row.update(pages=len(reader.pages), bibliography_present="Reproducible bibliography verification" in text,
                    text_present="Real sandbox verification" in text, graphic_present=any(page.get("/Resources", {}).get("/XObject") for page in reader.pages), pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest())
                row["passed"] = all(row[key] for key in ["bibliography_present", "text_present", "graphic_present"])
                shutil.copy2(pdf_path, out / f"REAL-TEX-{engine}-{bibliography}.pdf")
            else:
                row["passed"] = False
            report["results"].append(row)
    report["passed"] = all(row["passed"] for row in report["results"])
    (out / "REAL-TEX-VERIFICATION.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "results": [{k: v for k, v in r.items() if k != "log"} for r in report["results"]]}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
