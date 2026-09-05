import asyncio
import base64
import json
import os
import sys
from datetime import timedelta

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from oppenproject.config import APP_NAME, ROOT


@pytest.mark.parametrize("discussion_mode", ["off", "write"])
async def test_real_stdio_process_governance_boundary_and_no_oauth(tmp_path, discussion_mode):
    project = tmp_path / "projects/示例项目"
    project.mkdir(parents=True)
    (project / "project.md").write_text(
        "<!-- stepwise-r-project:v3 -->\n# searchable-governance\n",
        encoding="utf-8",
    )
    for path in ["Data/raw.csv", "README.md", "Audit/Functions/audit_model.Rmd"]:
        target = project / path
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_text("PRIVATE_CONTENT_NEVER_EXPOSE", encoding="utf-8")
    config = tmp_path / "config.local.json"
    (tmp_path / ".env").write_text(
        'OPPEN_TRANSPORT=stdio\nOPPEN_SCAN_ROOTS=["./projects"]\nOPPEN_STATE_DIR=.runtime\n'
        + f"OPPEN_DISCUSSION_MODE={discussion_mode}\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPPEN_")}
    env["PYTHONUTF8"] = "1"
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "run.py"), "--config", str(config), "serve"],
        env=env,
    )
    async with asyncio.timeout(30), stdio_client(server) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session:
            assert (await session.initialize()).serverInfo.name == APP_NAME
            listed = await session.list_tools()
            assert len(listed.tools) == (12 if discussion_mode == "write" else 8)
            for tool in listed.tools:
                assert tool.model_dump(by_alias=True)["securitySchemes"] == [{"type": "noauth"}]
                assert tool.meta["securitySchemes"] == [{"type": "noauth"}]
            for _ in range(50):
                result = await session.call_tool("list_projects")
                if result.structuredContent["projects"]:
                    break
                await asyncio.sleep(0.02)
            projects = result.structuredContent["projects"]
            assert len(projects) == 1
            pid = projects[0]["id"]
            overview = await session.call_tool("project_overview", {"project_id": pid})
            access = overview.structuredContent["discussion_access"]
            assert access["can_write"] == access["can_read"] == (discussion_mode == "write")
            assert access["granted_scopes"] is None and not access["missing_scopes"]
            assert overview.structuredContent["mcp_tools"] == [tool.name for tool in listed.tools]
            found = await session.call_tool("search", {"query": "searchable-governance"})
            hit = found.structuredContent["results"][0]
            assert hit["url"].startswith("oppen-steward://")
            fetched = await session.call_tool("fetch", {"id": hit["id"]})
            assert "searchable-governance" in fetched.structuredContent["text"]
            for path in ["Data/raw.csv", "README.md", "Audit/Functions/audit_model.Rmd", "../outside"]:
                for name, args in [
                    ("read_file", {"project_id": pid, "path": path, "encoding": "base64"}),
                    (
                        "fetch",
                        {"id": pid + ":" + base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")},
                    ),
                ]:
                    denied = await session.call_tool(name, args)
                    assert denied.isError and "PRIVATE_CONTENT_NEVER_EXPOSE" not in denied.model_dump_json()
            result = await session.call_tool("search", {"query": "PRIVATE_CONTENT_NEVER_EXPOSE"})
            assert not result.structuredContent["results"]
            result = await session.call_tool("list_files", {"project_id": pid})
            assert [item["path"] for item in result.structuredContent["entries"]] == ["project.md"]
            if discussion_mode == "write":
                created = await session.call_tool(
                    "create_discussion",
                    {
                        "project_id": pid,
                        "topic": "分析方案讨论",
                        "content": "第一版草稿",
                        "description": "讨论分析方案",
                        "request_id": "process-create-0001",
                    },
                )
                assert not created.isError
                doc = created.structuredContent
                assert doc["id"] == "D-000001"
                edited = await session.call_tool(
                    "edit_discussion",
                    {
                        "project_id": pid,
                        "discussion_id": doc["id"],
                        "content": "补充后的草稿",
                        "description": "补充分析方案",
                        "expected_revision": doc["revision"],
                        "request_id": "process-edit-000001",
                    },
                )
                assert not edited.isError
                assert (project / doc["path"]).read_text(encoding="utf-8") == "补充后的草稿"
    if discussion_mode == "off":
        assert not (tmp_path / ".runtime").exists()
    assert not (tmp_path / ".runtime/oauth.sqlite3").exists()
    assert not config.exists()
    # stdout was consumed by the SDK as JSON-RPC throughout, with no CLI banners.
    assert json.loads(json.dumps(projects))[0]["file_access"] == "governance-only"
