"""Exercise real HTTP OAuth and the official MCP client; never print credentials.

Run from the project root: .venv/bin/python -m tests.smoke_live
Only loopback verification is supported; never transmit the owner passphrase publicly.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from oppenproject.auth import COOKIE, SCOPE
from oppenproject.config import Settings


async def main(base_url, settings=None):
    if urlsplit(base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Owner-credential verification requires loopback transport")
    settings = settings or Settings.load()
    callback = "https://chatgpt.com/connector_platform_oauth_redirect"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    access = None
    async with httpx.AsyncClient(base_url=base_url, timeout=30, trust_env=False) as client:
        health = await client.get("/healthz")
        assert health.json()["service"] == "OppenSteward-MCP"
        assert (await client.get("/mcp")).status_code == 401
        resource = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert resource["resource"] == settings.resource
        response = await client.post(
            "/register",
            json={
                "client_name": "OppenSteward-MCP local verification",
                "redirect_uris": [callback],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": SCOPE,
            },
        )
        assert response.status_code == 201, "DCR failed"
        credentials = response.json()
        try:
            response = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": credentials["client_id"],
                    "redirect_uri": callback,
                    "scope": SCOPE,
                    "state": "live-verification",
                    "resource": settings.resource,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
            )
            location = urlsplit(response.headers["location"])
            # Only the current server's consent page is requested; never contact the ChatGPT callback.
            assert location.path == "/consent"
            page = await client.get(location.path + "?" + location.query)
            fields = dict(re.findall(r'type="hidden" name="([^"]+)" value="([^"]+)"', page.text))
            fields.update(
                password=(settings.state_dir / "owner-access.txt").read_text(encoding="utf-8").strip(),
                decision="allow",
            )
            headers = {"Origin": settings.public_url}
            if base_url.startswith("http://127.0.0.1:"):
                # Loopback test transport only: the production Secure cookie is explicitly replayed locally.
                headers["Cookie"] = f"{COOKIE}={page.cookies[COOKIE]}"
            response = await client.post("/consent", data=fields, headers=headers)
            assert response.status_code == 303, "Owner consent failed"
            query = parse_qs(urlsplit(response.headers["location"]).query)
            assert query["iss"] == [settings.public_url]
            response = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": credentials["client_id"],
                    "redirect_uri": callback,
                    "code": query["code"][0],
                    "code_verifier": verifier,
                    "resource": settings.resource,
                },
            )
            assert response.status_code == 200, "Token exchange failed"
            access = response.json()["access_token"]
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + access}, timeout=60, trust_env=False
            ) as transport:
                async with streamable_http_client(base_url + "/mcp", http_client=transport) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        tool_list = await session.list_tools()
                        catalog = await session.call_tool("list_projects", {})
                        discovery = catalog.structuredContent["discovery"]
                        result = await session.call_tool("list_projects", {"query": "OppenSteward-MCP"})
                        assert not result.isError and result.structuredContent
                        projects = result.structuredContent["projects"]
                        assert projects, "Discovery still running; retry after the first batch"
                        pid = projects[0]["id"]
                        overview = await session.call_tool("project_overview", {"project_id": pid})
                        assert not overview.isError
                        details = overview.structuredContent
                        assert details["mcp_tools"] == [tool.name for tool in tool_list.tools]
                        assert (
                            len(tool_list.tools)
                            == {"off": 8, "read": 10, "write": 12}[settings.discussion_mode]
                        )
                        assert details["discussion_access"]["granted_scopes"] == [SCOPE]
                        assert not details["discussion_access"]["can_write"]
                        if settings.discussion_mode != "off":
                            denied = await session.call_tool("list_discussions", {"project_id": pid})
                            assert denied.isError and "mcp/www_authenticate" in denied.meta
                        result = await session.call_tool(
                            "read_file", {"project_id": pid, "path": projects[0]["registry"]}
                        )
                        assert (
                            not result.isError
                            and "<!-- oppen-project-steward:v3 -->" in result.structuredContent["content"]
                        )
                        denied = await session.call_tool(
                            "read_file", {"project_id": pid, "path": "../outside"}
                        )
                        assert denied.isError
                        for blocked in ("README.md", "Data/raw.csv", "Audit/Runs/stage/current/results.csv"):
                            denied = await session.call_tool(
                                "read_file", {"project_id": pid, "path": blocked}
                            )
                            assert denied.isError
                        print(
                            json.dumps(
                                {
                                    "status": "passed",
                                    "transport": base_url,
                                    "server": initialized.serverInfo.name,
                                    "tools": len(tool_list.tools),
                                    "oauth": "DCR + owner consent + S256 PKCE + resource binding",
                                    "file_read": projects[0]["registry"],
                                    "traversal_denied": True,
                                    "projects_found": catalog.structuredContent["total"],
                                    "discovery_status": discovery["status"],
                                    "scan_in_progress": discovery.get("bounded", True),
                                    "unreadable_directories": discovery.get("error_count", 0),
                                }
                            )
                        )
        finally:
            if access:
                response = await client.post(
                    "/revoke", data={"client_id": credentials["client_id"], "token": access}
                )
                assert response.status_code == 200, "Test authorization revocation failed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8766")
    asyncio.run(main(parser.parse_args().base_url.rstrip("/")))
