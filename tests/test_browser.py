"""Native browser form test using only a temporary owner password on loopback.

Enable with OPPENPROJECT_PLAYWRIGHT=/path/to/node_modules/playwright.
The callback uses another loopback port to test cross-origin redirects without contacting ChatGPT.
"""

import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn

from oppenproject.server import create_app

from .smoke_live import main as protocol_smoke


@pytest.mark.skipif(not os.getenv("OPPENPROJECT_PLAYWRIGHT"), reason="Optional isolated browser verification")
def test_native_browser_consent_and_callback(settings, tmp_path):
    callback_headers = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            callback_headers.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Local verification callback")

        def log_message(self, *_):
            pass

    callback_server = ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
    callback_thread = threading.Thread(target=callback_server.serve_forever, daemon=True)
    callback_thread.start()
    callback = f"http://127.0.0.1:{callback_server.server_port}/callback"
    # Add an ordinary temporary project so the registered protocol verifier can also read a file.
    sample = Path(settings.scan_roots[0]) / "OppenProject"
    (sample / ".oppen-project-steward").mkdir(parents=True)
    (sample / ".oppen-project-steward/registry.md").write_text(
        "<!-- oppen-project-steward:v3 -->\n# OppenProject\n"
    )
    (sample / "README.md").write_text("# OppenProject temporary verification project\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        local = replace(
            settings, public_url=f"http://127.0.0.1:{port}", port=port, extra_redirect_uris=[callback]
        )
        server = uvicorn.Server(uvicorn.Config(create_app(local), access_log=False, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.02)
            assert server.started
            config = tmp_path / "browser-config.json"
            config.write_text(json.dumps({**asdict(local), "browser_test_callback": callback}, default=str))
            result = subprocess.run(
                [
                    "node",
                    str(Path(__file__).with_name("browser_oauth.cjs")),
                    "--playwright=" + os.environ["OPPENPROJECT_PLAYWRIGHT"],
                    "--config=" + str(config),
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout)
            assert report["browser_origin"] == local.public_url
            assert report["callback_redirect"] == "passed"
            assert report["pkce_exchange"] == 200
            assert callback_headers and not callback_headers[0].get("Referer")
            asyncio.run(protocol_smoke(local.public_url, local))
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            callback_server.shutdown()
            callback_server.server_close()
            callback_thread.join(timeout=5)
