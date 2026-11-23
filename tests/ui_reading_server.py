"""Isolated reading UI server: real XeLaTeX/CJK, no real model calls."""
import os
from ui_server import main

os.environ.update(PWA_TEX_SANDBOX_IMAGE='paperpilot-tex:local', PWA_LATEX_ENGINE='xelatex', PWA_BIB_ENGINE='auto', PWA_LATEX_TIMEOUT_SECONDS='60')

if __name__ == '__main__':
    main()
