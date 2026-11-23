"""Isolated UI server with deliberately slow builds for cross-paper scheduling."""
import time
from ui_server import main
import app
from tests.test_req011_sources_queue import fake_compiler

def slow_compiler(*args, **kwargs):
    time.sleep(6)
    return fake_compiler([])(*args, **kwargs)

app.compile_project_files = slow_compiler
if __name__ == '__main__':
    main()
