ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}
ARG APT_MIRROR=archive.ubuntu.com
ENV DEBIAN_FRONTEND=noninteractive
RUN if [ "$APT_MIRROR" != "archive.ubuntu.com" ]; then sed -i "s|archive.ubuntu.com|$APT_MIRROR|g;s|security.ubuntu.com|$APT_MIRROR|g" /etc/apt/sources.list; fi \
    && apt-get update && apt-get install -y --no-install-recommends \
    texlive-latex-base texlive-latex-recommended texlive-latex-extra \
    texlive-xetex texlive-lang-chinese texlive-bibtex-extra biber fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin texworker \
    && mkdir /workspace && chown 10001:10001 /workspace
USER 10001:10001
WORKDIR /workspace
ENV HOME=/tmp TEXMFOUTPUT=/workspace TEXMFVAR=/tmp/texmf-var TEXMFCACHE=/tmp/texmf-cache openin_any=p openout_any=p
CMD ["pdflatex", "--version"]
