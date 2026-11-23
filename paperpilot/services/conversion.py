from __future__ import annotations

import re


def svg_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def markdown_to_latex(markdown: str) -> str:
    text = re.sub(r"^### (.+)$", lambda m: "\\subsubsection{" + m.group(1) + "}", markdown, flags=re.M)
    text = re.sub(r"^## (.+)$", lambda m: "\\subsection{" + m.group(1) + "}", text, flags=re.M)
    text = re.sub(r"^# (.+)$", lambda m: "\\section{" + m.group(1) + "}", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: "\\textbf{" + m.group(1) + "}", text)
    return re.sub(r"`(.+?)`", lambda m: "\\texttt{" + m.group(1) + "}", text)


def latex_to_markdown(latex: str) -> str:
    text = re.sub(r"\\(sub)*section\{(.+?)\}", lambda m: "#" * (1 + m.group(0).count("sub")) + " " + m.group(2), latex)
    text = re.sub(r"\\textbf\{(.+?)\}", r"**\1**", text)
    text = re.sub(r"\\texttt\{(.+?)\}", r"`\1`", text)
    text = re.sub(r"\\begin\{document\}|\\end\{document\}|\\documentclass\{.+?\}|\\usepackage\[.+?\]\{.+?\}", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def bib_keys(bib: str) -> set[str]:
    return set(re.findall(r"@\w+\s*\{\s*([^,\s]+)", bib))


def ai_url(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"
