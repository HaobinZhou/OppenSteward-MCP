import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest

from oppenproject.auth import SCOPE, OAuthProvider, configure_owner, digest
from oppenproject.server import create_app

from .conftest import CALLBACK, begin, consent_form, login_code, register, rpc, token


def test_discovery_and_unauthenticated_denial(client, settings):
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == settings.resource
    assert resource["authorization_servers"] == [settings.public_url]
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == settings.public_url
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["scopes_supported"] == resource["scopes_supported"] == ["governance:read"]
    for path in ["/mcp", "/files/arbitrary/secret.txt"]:
        response = client.get(path)
        assert response.status_code == 401
        assert "oauth-protected-resource" in response.headers["www-authenticate"]


@pytest.mark.parametrize("method", ["none", "client_secret_post", "client_secret_basic"])
def test_pkce_flow_and_authenticated_mcp(client, method):
    credentials = register(client, method)
    form = login_code(client, credentials)
    if method == "client_secret_basic":
        form.pop("client_secret")
        response = client.post(
            "/token", data=form, auth=(credentials["client_id"], credentials["client_secret"])
        )
    else:
        response = client.post("/token", data=form)
    assert response.status_code == 200, response.text
    access = response.json()["access_token"]
    init = rpc(
        client,
        access,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert init["result"]["serverInfo"]["name"] == "OppenSteward-MCP"
    tools = rpc(client, access, "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "list_projects",
        "refresh_projects",
        "project_overview",
        "list_files",
        "read_file",
        "search",
        "fetch",
        "get_skill_guide",
    }
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    assert all(t["securitySchemes"] == [{"type": "oauth2", "scopes": [SCOPE]}] for t in tools)
    assert all(t["_meta"]["securitySchemes"] == t["securitySchemes"] for t in tools)
    result = rpc(client, access, "tools/call", {"name": "list_projects", "arguments": {}})["result"]
    assert not result.get("isError")


def test_bad_password_csrf_cookie_and_denial(client, settings):
    credentials = register(client)
    response, _ = begin(client, credentials)
    form = consent_form(client, response)
    form.update(password="wrong", decision="allow")
    assert client.post("/consent", data=form).status_code == 403
    assert (
        client.post(
            "/consent", data={**form, "csrf": "wrong"}, headers={"Origin": settings.public_url}
        ).status_code
        == 403
    )
    assert client.post("/consent", data=form, headers={"Origin": settings.public_url}).status_code == 401
    response = client.post(
        "/consent", data={**form, "decision": "deny"}, headers={"Origin": settings.public_url}
    )
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["error"] == ["access_denied"] and query["iss"] == [settings.public_url]
    assert client.post("/consent", data=form, headers={"Origin": settings.public_url}).status_code == 403


def test_consent_preserves_browser_origin_and_rejects_opaque_origin(client, settings):
    response, _ = begin(client, register(client))
    page = client.get(response.headers["location"])
    assert page.headers["referrer-policy"] == "same-origin"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert len(page.headers.get_list("content-security-policy")) == 1
    assert "form-action 'self' " + CALLBACK in page.headers["content-security-policy"]
    form = consent_form(client, response)
    form.update(
        password=(settings.state_dir / "owner-access.txt").read_text(encoding="utf-8").strip(),
        decision="allow",
    )
    for origin in ["null", "https://chatgpt.com", "https://evil.test"]:
        rejected = client.post("/consent", data=form, headers={"Origin": origin})
        assert rejected.status_code == 403
    assert client.post("/consent", data=form, headers={"Origin": settings.public_url}).status_code == 303


def test_form_csp_scopes_callback_to_current_client(client, settings):
    callback = "https://chatgpt.com/connector/oauth/test-client-callback"
    response, _ = begin(client, register(client, redirect=callback), redirect_uri=callback)
    page = client.get(response.headers["location"])
    policy = page.headers["content-security-policy"]
    assert "form-action 'self' " + callback + ";" in policy
    assert CALLBACK not in policy
    assert "*" not in policy
    form = consent_form(client, response)
    form.update(password="incorrect", decision="allow")
    retry = client.post("/consent", data=form, headers={"Origin": settings.public_url})
    assert retry.status_code == 401
    assert retry.headers["content-security-policy"] == policy


def test_wrong_pkce_resource_redirect_and_replay(client):
    credentials = register(client)
    form = login_code(client, credentials)
    for override in [
        {"code_verifier": "z" * 64},
        {"resource": "https://evil.test/mcp"},
        {"redirect_uri": "https://chatgpt.com/connector/oauth/other"},
    ]:
        assert client.post("/token", data={**form, **override}).status_code == 400
    assert client.post("/token", data=form).status_code == 200
    assert client.post("/token", data=form).status_code == 400


def test_code_is_atomic_single_use(client):
    form = login_code(client, register(client))
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: client.post("/token", data=form).status_code, range(2)))
    assert sorted(statuses) == [200, 400]


def test_authorize_errors_include_issuer_and_reject_bad_resource(client, settings):
    response, _ = begin(client, register(client), resource="https://evil.test/mcp")
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["error"] == ["invalid_request"]
    assert params["iss"] == [settings.public_url]


def test_refresh_persistence_rotation_replay_and_revocation(client, settings):
    credentials = register(client)
    initial = token(client, credentials)
    # Recreating the provider reads persistent credentials, not in-memory session state.
    provider = OAuthProvider(settings)
    assert asyncio.run(provider.verify_token(initial["access_token"]))
    form = {
        "grant_type": "refresh_token",
        "client_id": credentials["client_id"],
        "refresh_token": initial["refresh_token"],
        "resource": settings.resource,
    }
    assert client.post("/token", data={**form, "scope": "projects:write"}).status_code == 400
    refreshed = client.post("/token", data=form)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["refresh_token"] != initial["refresh_token"]
    assert client.post("/token", data=form).status_code == 400
    assert asyncio.run(provider.verify_token(refreshed.json()["access_token"])) is None


def test_revoke_endpoint_expiry_and_password_rotation(client, settings):
    credentials = register(client)
    issued = token(client, credentials)
    response = client.post(
        "/revoke",
        data={
            "client_id": credentials["client_id"],
            "token": issued["refresh_token"],
            "token_type_hint": "refresh_token",
        },
    )
    assert response.status_code == 200
    assert asyncio.run(client.app.state.provider.verify_token(issued["access_token"])) is None
    issued = token(client)
    provider = client.app.state.provider
    value = provider.store.get("access", digest(issued["access_token"]))
    provider.store.put("access", digest(issued["access_token"]), value, time.time() - 1)
    assert asyncio.run(provider.verify_token(issued["access_token"])) is None
    issued = token(client)
    configure_owner(settings, rotate=True)
    assert asyncio.run(provider.verify_token(issued["access_token"])) is None


def test_host_origin_redirect_and_request_limits(client):
    assert client.get("/healthz", headers={"Host": "evil.test"}).status_code == 400
    assert client.get("/healthz", headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/register", content=b"x" * 1_048_577).status_code == 413
    assert (
        client.post("/register", content=b"{bad", headers={"content-type": "application/json"}).status_code
        == 400
    )
    for redirect in [
        "https://chatgpt.com.evil.test/connector_platform_oauth_redirect",
        "https://evil.test/callback",
        CALLBACK + "?next=https://evil.test",
    ]:
        response = client.post(
            "/register",
            json={"redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"]},
        )
        assert response.status_code == 400


def test_issuer_change_revokes_old_tokens(client, settings):
    issued = token(client)
    settings.public_url = "https://changed.example.test"
    provider = create_app(settings).state.provider
    assert asyncio.run(provider.verify_token(issued["access_token"])) is None


def test_broad_scope_upgrade_revokes_all_old_grants_and_preserves_password(client, settings):
    credentials = register(client)
    issued = token(client, credentials)
    pending_code = login_code(client, credentials)
    provider = client.app.state.provider
    password_hash = provider.store.get("meta", "owner_password")
    provider.store.put("meta", "access_scope", "projects:read")
    upgraded = OAuthProvider(settings)
    assert upgraded.store.get("meta", "access_scope") == SCOPE
    assert upgraded.store.get("meta", "owner_password") == password_hash
    assert asyncio.run(upgraded.verify_token(issued["access_token"])) is None
    assert asyncio.run(upgraded.load_refresh_token(None, issued["refresh_token"])) is None
    assert asyncio.run(upgraded.get_client(credentials["client_id"])) is None
    assert upgraded.store.get("code", digest(pending_code["code"])) is None
    assert (
        client.post("/register", json={"redirect_uris": [CALLBACK], "scope": "projects:read"}).status_code
        == 400
    )
    response, _ = begin(client, register(client))
    page = client.get(response.headers["location"])
    assert "治理文件" in page.text and "governance:read" in page.text
    assert "数据、源码、Results、Deliverables、Audit、README" in page.text


def test_mcp_trailing_slash_never_redirects_or_bypasses_auth(client):
    for method in ["GET", "POST"]:
        response = client.request(method, "/mcp/")
        assert response.status_code == 401
        assert "location" not in response.headers
        assert f'scope="{SCOPE}"' in response.headers["www-authenticate"]
    access = token(client)["access_token"]
    headers = {"Authorization": "Bearer " + access, "Accept": "application/json, text/event-stream"}
    for method, params in [
        (
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        ),
        ("tools/list", {}),
    ]:
        response = client.post(
            "/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        assert response.status_code == 200 and "result" in response.json()
        assert "location" not in response.headers
    assert client.get("/mcp/", headers={"Origin": "https://evil.test"}).status_code == 403


def test_http_diagnostics_do_not_log_credentials_content_or_file_paths(client, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="oppenproject.http")
    secret = "DO_NOT_LOG_THIS_CREDENTIAL_OR_CONTENT"
    client.post(
        "/consent?code=" + secret,
        headers={"Origin": "https://untrusted.example/" + secret},
        data={"password": secret},
    )
    client.get("/files/private-project/private-patient.csv", headers={"Authorization": "Bearer " + secret})
    client.get("/mcp/?state=" + secret)
    messages = [r.getMessage() for r in caplog.records if r.name == "oppenproject.http"]
    assert len(messages) == 3
    assert secret not in str(messages) and "private-patient" not in str(messages)
    assert "private-project" not in str(messages) and "untrusted.example" not in str(messages)
    assert '"route": "/files/*"' in messages[1]
    assert '"mcp_slash_alias": true' in messages[2]
