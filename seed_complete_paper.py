"""Create a coherent, compilable paper fixture in the local PaperPilot app.

The reported results are explicitly synthetic so this fixture is safe for product
testing and does not present unverified scientific claims as real research.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8765"


def call(method: str, path: str, *, timeout: int = 30, **kwargs):
    response = requests.request(method, BASE + path, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response.json()


def put_file(pid: str, path: str, content: str) -> None:
    call("PUT", f"/api/projects/{pid}/files", json={"path": path, "content": content})


MAIN_TEX = r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb,booktabs,geometry,hyperref}
\geometry{margin=1in}
\title{A Reproducible Evaluation Framework for Interpretable Machine Learning on Tabular Data}
\author{PaperPilot Test Research Group}
\date{October 2026}

\begin{document}
\maketitle

\begin{abstract}
Interpretability methods are often compared using incompatible data splits, metrics,
and explanation budgets. This paper presents a reproducible evaluation framework for
tabular classification that separates predictive quality, explanation fidelity, and
human-facing stability. The framework combines a predictive model with permutation
importance, SHAP explanations, and a local surrogate audit. We define a fixed
benchmark protocol, an explanation stability score, and a reporting template that
keeps model and explanation claims separate. On a synthetic benchmark designed to
exercise the protocol, the reference implementation reaches 0.86 AUROC while the
best explanation method obtains 0.91 fidelity and 0.88 stability. These numbers are
fixture values for software testing, not claims about a real dataset. The main
contribution is an end-to-end paper workflow that can be reproduced from the files
in this project.
\end{abstract}

\section{Introduction}
\input{sections/introduction}

\section{Related Work}
\input{sections/related_work}

\section{Methods}
\input{sections/methods}

\section{Experimental Setup}
\input{sections/experimental_setup}

\section{Results}
\input{sections/results}

\section{Discussion}
\input{sections/discussion}

\section{Conclusion}
\input{sections/conclusion}

\bibliographystyle{plain}
\bibliography{references}
\end{document}
"""

SECTIONS = {
    "sections/introduction.tex": r"""Interpretability is used to diagnose models, communicate decisions, and identify
data collection failures. However, explanation quality is frequently reported as a
single qualitative observation. This makes it difficult to compare methods or to
reproduce a published result. The problem is especially visible in tabular data,
where correlated variables can make feature rankings change across small changes in
the training set.

We make three contributions. First, we define a protocol that fixes data splits,
random seeds, evaluation budgets, and the order in which explanations are computed.
Second, we introduce a stability score that measures whether an explanation preserves
its top-ranked features under bootstrap perturbations. Third, we provide a complete
project fixture containing source files, a bibliography, an outline, a terminology
library, and a logic chain. This makes the workflow itself testable in addition to
the numerical example.

Our research question is: can a small, explicit evaluation protocol expose meaningful
trade-offs between predictive performance and explanation quality? The answer in this
fixture is yes, but the reported values are synthetic and should only be used to
exercise the writing application.
""",
    "sections/related_work.tex": r"""Random forests established a strong baseline for tabular prediction and also
introduced a convenient permutation-based view of feature importance
\cite{breiman2001}. Local surrogate explanations such as LIME approximate a complex
predictor near one example \cite{ribeiro2016}, while SHAP assigns contributions using
cooperative game-theoretic values \cite{lundberg2017}. These approaches answer
different questions: a global ranking describes average behavior, whereas a local
explanation describes one prediction.

Stability is related to, but distinct from, fidelity. A method may fit a local model
well while changing its selected features under a small perturbation. Conversely, a
stable ranking can faithfully reproduce a systematic bias. We therefore report
fidelity and stability as separate axes. The evaluation design also follows the
principle that a benchmark must define its split and metric before a method is
selected, a practice consistent with standard empirical methodology
\cite{kohavi1995}.
""",
    "sections/methods.tex": r"""\subsection{Problem formulation}
Let $D=\{(x_i,y_i)\}_{i=1}^{n}$ be a binary classification dataset with
$x_i\in\mathbb{R}^{p}$. A predictor $f$ produces a probability $\hat{p}_i=f(x_i)$.
We evaluate predictive quality with AUROC and Brier score. For an explanation method
$e$, the explanation of sample $i$ is a ranked list $e(x_i)=(r_{i1},\ldots,r_{ip})$.

\subsection{Explanation measures}
Fidelity is measured by fitting a sparse local surrogate $g_i$ to perturbations of
$x_i$ and computing the agreement between $g_i$ and $f$. We use the mean squared
probability error:
\[
  \operatorname{Fid}(e)=1-\frac{1}{m}\sum_{j=1}^{m}
  \left(f(z_{ij})-g_i(z_{ij})\right)^2.
\]
For stability, let $T_i^{(b)}$ be the top-$k$ features from bootstrap replicate $b$.
We define the pairwise Jaccard stability score as
\[
  \operatorname{Stab}(e)=\frac{2}{B(B-1)}\sum_{b<c}
  \frac{|T_i^{(b)}\cap T_i^{(c)}|}{|T_i^{(b)}\cup T_i^{(c)}|},
\]
then average over audited samples. The score is in $[0,1]$; larger values indicate
more consistent feature selections.

\subsection{Reference pipeline}
The reference pipeline standardizes continuous features using training data only,
fits a random forest, computes permutation importance, and audits the top ten test
examples with SHAP and a local surrogate. Every stochastic component uses seed 17.
The pipeline writes metrics and explanation rankings to a project-local artifact so
that the paper can be regenerated without mixing resources from another project.
""",
    "sections/experimental_setup.tex": r"""We use a synthetic binary classification fixture with 2,400 examples and 18
features. Six features carry signal, four are correlated distractors, and the
remaining features are noise. The data generator is deterministic and is included
only as a product-test specification; it is not intended as a scientific dataset.

We reserve 60\% of examples for training, 20\% for validation, and 20\% for testing,
with stratification at each split. The predictive model is a random forest with 300
trees and a minimum leaf size of two. The explanation budget is ten audited test
examples, 40 perturbations per example, and 20 bootstrap replicates. We compare
permutation importance, SHAP, and a sparse local surrogate using the same audited
examples. Table~\ref{tab:protocol} summarizes the fixed protocol.

\begin{table}[ht]
\centering
\caption{Deterministic evaluation protocol used by the fixture.}
\label{tab:protocol}
\begin{tabular}{ll}
\toprule
Item & Setting \\
\midrule
Data size & 2,400 examples, 18 features \\
Split & 60/20/20 stratified \\
Random seed & 17 \\
Predictor & Random forest, 300 trees \\
Audit budget & 10 examples, 20 bootstrap replicates \\
Primary metrics & AUROC, Brier, fidelity, stability \\
\bottomrule
\end{tabular}
\end{table}
""",
    "sections/results.tex": r"""Table~\ref{tab:results} reports the synthetic reference values. The predictor
achieves an AUROC of 0.86 and a Brier score of 0.142. SHAP produces the strongest
local fidelity, while the sparse surrogate is slightly more stable under bootstrap
perturbations. Permutation importance is easy to summarize globally but has lower
local fidelity when correlated distractors are present.

\begin{table}[ht]
\centering
\caption{Synthetic results included to test tables and citation-aware writing.}
\label{tab:results}
\begin{tabular}{lrrrr}
\toprule
Method & AUROC & Brier & Fidelity & Stability \\
\midrule
Permutation importance & 0.86 & 0.142 & 0.78 & 0.73 \\
SHAP & 0.86 & 0.142 & 0.91 & 0.88 \\
Sparse surrogate & 0.86 & 0.142 & 0.87 & 0.90 \\
\bottomrule
\end{tabular}
\end{table}

The difference between SHAP and the sparse surrogate illustrates why one explanation
score is insufficient. A user who values local agreement may prefer SHAP, whereas a
user who needs a repeatable shortlist may prefer the sparse surrogate. These values
are intentionally small and regular so that a test can detect accidental formatting
or conversion changes.
""",
    "sections/discussion.tex": r"""The fixture supports two conclusions about the protocol. First, explanation
quality has multiple dimensions: fidelity and stability need not rank methods in the
same order. Second, a reproducible report must preserve the connection between a
claim, its metric, and the data-generation assumptions. The terminology library and
logic chain in this project make those relationships explicit for the writing tool.

There are three limitations. The benchmark is synthetic, the predictor family is
restricted to one tabular baseline, and the stability score only audits the top-$k$
feature set. The protocol also does not establish causal validity. A future study
should evaluate real datasets, multiple model families, and human decision quality.
""",
    "sections/conclusion.tex": r"""We presented a complete, deterministic paper fixture for evaluating interpretable
tabular models. The protocol separates predictive quality, explanation fidelity, and
stability, and the project stores the source files and supporting knowledge needed to
reproduce the narrative. The numerical results are synthetic by design. The fixture
therefore provides a safe end-to-end test for editing, AI-assisted writing, resource
tracking, citation comparison, LaTeX compilation, and PDF preview.
""",
}

REFERENCES = r"""@article{breiman2001,
  author = {Breiman, Leo},
  title = {Random Forests},
  journal = {Machine Learning},
  year = {2001},
  volume = {45},
  number = {1},
  pages = {5--32}
}

@inproceedings{ribeiro2016,
  author = {Ribeiro, Marco Tulio and Singh, Sameer and Guestrin, Carlos},
  title = {Why Should I Trust You? Explaining the Predictions of Any Classifier},
  booktitle = {Proceedings of the 22nd ACM SIGKDD International Conference on Knowledge Discovery and Data Mining},
  year = {2016},
  pages = {1135--1144}
}

@inproceedings{lundberg2017,
  author = {Lundberg, Scott M. and Lee, Su-In},
  title = {A Unified Approach to Interpreting Model Predictions},
  booktitle = {Advances in Neural Information Processing Systems},
  year = {2017},
  volume = {30}
}

@article{kohavi1995,
  author = {Kohavi, Ron},
  title = {A Study of Cross-Validation and Bootstrap for Accuracy Estimation and Model Selection},
  journal = {Proceedings of the 14th International Joint Conference on Artificial Intelligence},
  year = {1995},
  volume = {2},
  pages = {1137--1143}
}
"""


def main() -> None:
    project = call(
        "POST",
        "/api/projects",
        json={
            "name": "完整测试论文：可解释机器学习评估",
            "domain": "机器学习 / 可解释人工智能",
            "style": "学术严谨、结果可复现",
            "citation_style": "plain",
            "model": "deepseek-v4-pro",
        },
    )
    pid = project["project"]["id"]
    put_file(pid, "main.tex", MAIN_TEX)
    put_file(pid, "references.bib", REFERENCES)
    for path, content in SECTIONS.items():
        put_file(pid, path, content)

    outline = """1. Introduction\n   1.1 Motivation and reproducibility gap\n   1.2 Contributions and research question\n2. Related Work\n3. Methods\n   3.1 Problem formulation\n   3.2 Fidelity and stability metrics\n   3.3 Reference pipeline\n4. Experimental Setup\n5. Results\n6. Discussion and limitations\n7. Conclusion"""
    call("PUT", f"/api/projects/{pid}/outline", json={"content": outline})

    terms = [
        ("interpretability", "The degree to which a human can understand a model or explanation.", "可解释性,透明性"),
        ("SHAP", "Shapley-value based feature attribution for model predictions.", "SHapley Additive exPlanations"),
        ("fidelity", "Agreement between an explanation surrogate and the original predictor.", "忠实度"),
        ("stability", "Consistency of an explanation under controlled perturbations.", "稳定性,robustness"),
        ("calibration", "Agreement between predicted probabilities and observed frequencies.", "概率校准"),
    ]
    for term, definition, aliases in terms:
        call("POST", f"/api/projects/{pid}/terms", json={"term": term, "definition": definition, "aliases": aliases})

    corpus = [
        ("Synthetic benchmark specification", "A deterministic 2,400-example binary classification fixture with 18 features, six signal features, four correlated distractors, and eight noise features.", "fixture design"),
        ("Evaluation protocol", "Use a stratified 60/20/20 split, random seed 17, ten audited examples, 20 bootstrap replicates, and AUROC, Brier, fidelity, and stability metrics.", "experimental setup"),
        ("Scope and limitations", "All reported values are synthetic and exist only to test the writing workflow; they are not evidence for a real-world scientific claim.", "discussion notes"),
    ]
    for title, content, source in corpus:
        call("POST", f"/api/projects/{pid}/corpus", json={"title": title, "content": content, "source": source})

    refs = [
        ("breiman2001", "Random Forests", "Leo Breiman", "2001"),
        ("ribeiro2016", "Why Should I Trust You? Explaining the Predictions of Any Classifier", "Marco Tulio Ribeiro; Sameer Singh; Carlos Guestrin", "2016"),
        ("lundberg2017", "A Unified Approach to Interpreting Model Predictions", "Scott M. Lundberg; Su-In Lee", "2017"),
        ("kohavi1995", "A Study of Cross-Validation and Bootstrap for Accuracy Estimation and Model Selection", "Ron Kohavi", "1995"),
    ]
    for key, title, authors, year in refs:
        call("POST", f"/api/projects/{pid}/refs", json={"cite_key": key, "title": title, "authors": authors, "year": year})

    logic = [
        ("claim", "A fixed protocol improves reproducibility", "Split, seed, budget, and metric are fixed before comparison.", "evidence-protocol"),
        ("evidence", "Fidelity and stability rank methods differently", "The synthetic results table shows SHAP leading fidelity while the sparse surrogate leads stability.", "claim-tradeoff"),
        ("conclusion", "Explanation quality is multidimensional", "A single explanation score cannot represent all user needs.", "claim-tradeoff"),
    ]
    for kind, label, details, links in logic:
        call("POST", f"/api/projects/{pid}/logic", json={"kind": kind, "label": label, "details": details, "links": links})

    compile_result = call("POST", f"/api/projects/{pid}/compile", timeout=120)
    compare_result = call("POST", f"/api/projects/{pid}/compare")
    payload = call("GET", f"/api/projects/{pid}")
    result = {
        "project_id": pid,
        "title": payload["project"]["name"],
        "files": [item["path"] for item in payload["files"]],
        "terms": len(payload["terms"]),
        "corpus": len(payload["corpus"]),
        "references": len(payload["refs"]),
        "logic_nodes": len(payload["logic"]),
        "compile_ok": compile_result.get("ok"),
        "pdf_url": compile_result.get("pdf_url"),
        "citation_summary": compare_result.get("summary"),
        "missing_citations": compare_result.get("missing"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not compile_result.get("ok") or compare_result.get("missing"):
        sys.exit(1)


if __name__ == "__main__":
    main()
