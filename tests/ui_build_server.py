"""Isolated UI server with actual local TeX, explicit AI test double, no user DB."""
import os
from ui_server import main

os.environ['PWA_TEX_SANDBOX_IMAGE'] = 'paperpilot-tex:local'
os.environ['PWA_LATEX_ENGINE'] = 'pdflatex'
os.environ['PWA_BIB_ENGINE'] = 'auto'
os.environ['PWA_LATEX_TIMEOUT_SECONDS'] = '15'

if __name__ == '__main__':
    main()
