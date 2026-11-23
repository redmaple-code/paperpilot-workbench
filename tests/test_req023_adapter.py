from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from paperpilot.adapters.visual_image import generate
from paperpilot.services.project_errors import ProjectServiceError
from tests.test_req022_assets import png


def test_actual_http_image_generation_and_multipart_edits():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.path, self.headers.get("Content-Type"), body))
            data = json.dumps({"data": [{"b64_json": base64.b64encode(png()).decode()}], "usage": {"images": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    admission = {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "auth_mode": "none", "model": "image-test"}
    spec = {"prompt": "scientific illustration", "size": "1024x1024"}
    try:
        first = generate(admission, spec, {"values": {"background": "white"}})
        assert first["content"] == png() and first["usage"] == {"images": 1,'external_calls':1}
        second = generate(admission, spec, {"values": {}}, reference=("reference.png", png(), "image/png"), mask=("mask.png", png(), "image/png"))
        assert second["content"] == png()
        assert calls[0][0] == "/v1/images/generations" and b'"n": 1' in calls[0][2]
        assert calls[1][0] == "/v1/images/edits" and calls[1][1].startswith("multipart/form-data")
        assert b'name="image"' in calls[1][2] and b'name="mask"' in calls[1][2]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_environment_secret_never_sent_to_other_provider(monkeypatch):
    monkeypatch.setenv("PWA_AI_BASE_URL", "https://expected.invalid/v1")
    monkeypatch.setenv("PWA_AI_API_KEY", "test-value-never-printed")
    calls = []
    with pytest.raises(ProjectServiceError) as exc:
        generate({"base_url": "https://other.invalid/v1", "auth_mode": "environment", "model": "image"}, {"prompt": "x"}, {"values": {}}, request_post=lambda *a, **k: calls.append(1))
    assert exc.value.code == "credential_endpoint_mismatch" and calls == []


def test_vision_response_requires_advisory_status_and_marks_missing_checks():
    from paperpilot.adapters.visual_vision import analyze
    calls = []
    response_findings = [{"check": "semantic", "status": "needs_human_review", "message": "Cannot verify data", "location": "panel A"}]
    class Response:
        status_code = 200
        def iter_content(self, size):
            yield json.dumps({"choices": [{"message": {"content": json.dumps({"findings": response_findings})}}], "usage": {"total_tokens": 300}}).encode()
        def close(self):
            pass
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()
    admission = {"base_url": "http://127.0.0.1:9999/v1", "auth_mode": "none", "model": "vision-test"}
    admission['model_limits'] = {'vision-test': {'input_tokens': 100000, 'output_tokens': 10000, 'source': 'isolated protocol fixture',
        'vision': {'tokens_per_image': 1000, 'max_width': 1600, 'max_height': 1600, 'source': 'isolated protocol fixture, high detail'}}}
    result = analyze(admission, [png()], ["semantic", "readability"], request_post=post)
    assert result["findings"][1]["status"] == "unknown"
    assert calls[0][0].endswith("/chat/completions")
    assert calls[0][1]["json"]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    response_findings[0]["status"] = "pass"
    with pytest.raises(ProjectServiceError) as exc:
        analyze(admission, [png()], ["semantic"], request_post=post)
    assert exc.value.code == "provider_unknown"
