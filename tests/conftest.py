import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from oppenproject.auth import SCOPE, configure_owner
from oppenproject.catalog import STEWARD
from oppenproject.config import Settings
from oppenproject.server import create_app

CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    project = root / "示例项目"
    (project / ".oppen-project-steward").mkdir(parents=True)
    (project / ".oppen-project-steward/registry.md").write_text(STEWARD + "\n# Registry\n")
    memory = project / ".oppen-project-steward/Memory"
    (memory / "entries").mkdir(parents=True)
    (memory / "index.md").write_text(
        "<!-- oppen-project-steward:memory-index -->\n"
        "| ID | Title | Status | Related Topics |\n"
        "| M-0001 | Example | active | - |\n"
    )
    (memory / "entries/M-0001.md").write_text("# Example\n检索 alpha\nline three\n")
    (project / "notes.md").write_text("private-project-content\n")
    settings = Settings(
        public_url="https://project.example.test",
        scan_roots=[str(root)],
        state_dir=tmp_path / "state",
        scan_interval=3600,
    )
    configure_owner(settings)
    return settings


@pytest.fixture
def client(settings):
    app = create_app(settings)
    app.state.catalog.refresh()
    with TestClient(app, base_url=settings.public_url, follow_redirects=False) as client:
        yield client


def register(client, method="none", redirect=CALLBACK):
    response = client.post(
        "/register",
        json={
            "client_name": "ChatGPT test",
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": method,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": SCOPE,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def begin(client, credentials, **overrides):
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {
        "client_id": credentials["client_id"],
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "scope": SCOPE,
        "state": "test-state",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": client.app.state.provider.settings.resource,
        **overrides,
    }
    response = client.get("/authorize", params=params)
    return response, verifier


def consent_form(client, response):
    page = client.get(response.headers["location"])
    assert page.status_code == 200, page.text
    return dict(re.findall(r'type="hidden" name="([^"]+)" value="([^"]+)"', page.text))


def login_code(client, credentials):
    response, verifier = begin(client, credentials)
    form = consent_form(client, response)
    settings = client.app.state.provider.settings
    form.update(password=(settings.state_dir / "owner-access.txt").read_text().strip(), decision="allow")
    response = client.post("/consent", data=form, headers={"Origin": settings.public_url})
    assert response.status_code == 303, response.text
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["state"] == ["test-state"]
    assert params["iss"] == [settings.public_url]
    return {
        "grant_type": "authorization_code",
        "client_id": credentials["client_id"],
        "redirect_uri": CALLBACK,
        "code": params["code"][0],
        "code_verifier": verifier,
        "resource": settings.resource,
        **({"client_secret": credentials["client_secret"]} if credentials.get("client_secret") else {}),
    }


def token(client, credentials=None):
    response = client.post("/token", data=login_code(client, credentials or register(client)))
    assert response.status_code == 200, response.text
    return response.json()


def rpc(client, bearer, method, params=None, id=1):
    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + bearer, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()
