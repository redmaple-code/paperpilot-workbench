"""Run imported PR1 browser suites against their owned, isolated fixture servers."""
import argparse
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('suite', choices=['business', 'conversations', 'scholar'])
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        port = connection.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='paperpilot-pr1-merge-') as storage:
        servers = {'business': 'tests/ui_pr1_business_server.py', 'conversations': 'tests/ui_agent_server.py', 'scholar': 'tests/ui_scholar_server.py'}
        browsers = {'business': 'tests/browser_pr1_author_journey.py', 'conversations': 'tests/browser_agent_conversations.py', 'scholar': 'tests/browser_scholar_workspace.py'}
        server, browser = servers[args.suite], browsers[args.suite]
        command = [sys.executable, '-X', 'utf8', server, '--port', str(port)]
        if args.suite in {'business', 'scholar'}:
            command.extend(['--storage', storage])
        with (output / 'server.log').open('w', encoding='utf8') as log:
            process = subprocess.Popen(command, stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                url = f'http://127.0.0.1:{port}'
                for _ in range(200):
                    if process.poll() is not None:
                        raise RuntimeError('Fixture server exited. See server.log.')
                    try:
                        with urllib.request.urlopen(url, timeout=.5):
                            break
                    except (OSError, TimeoutError):
                        time.sleep(.2)
                else:
                    raise TimeoutError('Fixture server did not start.')
                result = subprocess.run([sys.executable, '-X', 'utf8', browser,
                                         '--base-url', url, '--output', str(output)])
                return result.returncode
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    sys.exit(main())
