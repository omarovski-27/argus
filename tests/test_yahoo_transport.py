"""Verify server-selected encodings cannot reach curl's Brotli decoder."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from shared.yahoo import yahoo_session


@pytest.fixture
def server():
    # Valid Brotli encoding of 'Brotli audit: ordinary valid response.'.
    payload = bytes.fromhex(
        "8b128042726f746c692061756469743a206f7264696e6172792076616c696420726573706f6e73652e03"
    )
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Accept-Encoding"))
            body = payload if self.path == "/br" else b'{"ok": true}'
            self.send_response(200)
            if self.path == "/br":
                self.send_header("Content-Encoding", "br")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", payload, seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_unsolicited_brotli_is_not_decoded(server):
    url, payload, seen = server
    with yahoo_session() as session:
        session.trust_env = False
        response = session.get(url + "/br", timeout=5)
    assert response.content == payload
    assert seen == ["identity"]
    with pytest.raises((ValueError, UnicodeDecodeError)):
        json.loads(response.content)


def test_uncompressed_json_still_works(server):
    url, _payload, seen = server
    with yahoo_session() as session:
        session.trust_env = False
        response = session.get(url + "/json", timeout=5)
    assert response.json() == {"ok": True}
    assert seen == ["identity"]
