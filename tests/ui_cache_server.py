"""Isolated UI server for build retention; compiler is a deterministic fixture."""
from ui_server import main
import app
from tests.test_req011_sources_queue import fake_compiler
app.compile_project_files = fake_compiler([])
if __name__ == '__main__':
    main()
