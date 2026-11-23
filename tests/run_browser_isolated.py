"""Own the exact test-server process, including cleanup on Windows."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('script')
    parser.add_argument('--output', required=True)
    parser.add_argument('--workspace-copy', type=Path, help='Directory with paperpilot.db and projects to copy into the test workspace')
    args = parser.parse_args()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'browser-server.log').open('w', encoding='utf-8') as log:
        server = [sys.executable, '-X', 'utf8', 'tests/ui_server.py', '--port', str(port)]
        if args.workspace_copy:
            server.extend(['--workspace-copy', str(args.workspace_copy.resolve(strict=True))])
        process = subprocess.Popen(server,
            stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            url = f'http://127.0.0.1:{port}'
            for _ in range(200):
                if process.poll() is not None:
                    raise RuntimeError('isolated UI server exited; see browser-server.log')
                try:
                    with urllib.request.urlopen(url, timeout=.5) as response:
                        if response.status == 200:
                            break
                except (OSError, TimeoutError):
                    time.sleep(.2)
            else:
                raise TimeoutError('isolated UI server not ready')
            result = subprocess.run([sys.executable, '-X', 'utf8', args.script, '--base-url', url, '--output', str(output)])
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
