"""The Freeze MCP boundary is distinct from free-form Discussion and Canonical."""

import os
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from oppenproject.auth import FREEZE_READ, FREEZE_WRITE, SCOPE
from oppenproject.catalog import AccessDenied, Catalog
from oppenproject.freeze import Freezes
from oppenproject.server import create_app, create_mcp

from .conftest import rpc, token


def skill_root():
    configured = os.environ.get("STEPWISE_R_TEST_SKILL_ROOT")
    if configured:
        root = Path(configured)
        if not (root / "stepwise-r-project/scripts/freeze_store.py").is_file():
            pytest.fail("STEPWISE_R_TEST_SKILL_ROOT lacks the companion Freeze helper")
        return root
    sibling = Path(__file__).resolve().parents[2] / "Academic_skill" / "academic-skills"
    if (sibling / "stepwise-r-project/scripts/freeze_store.py").is_file():
        return sibling
    installed = Path.home() / ".codex" / "skills"
    if (installed / "stepwise-r-project/scripts/freeze_store.py").is_file():
        return installed
    pytest.skip("The companion Stepwise R skill is not installed")


def stepwise(settings):
    project = settings.projects_file.parent / "projects" / "示例项目"
    (project / ".oppen-project-steward/registry.md").unlink()
    (project / "project.md").write_text("<!-- stepwise-r-project:v3 -->\n", encoding="utf-8")
    settings.skill_root = skill_root()
    settings.freeze_mode = "write"
    settings.freeze_projects = [str(project)]
    catalog = Catalog(settings)
    catalog.refresh(force=True)
    return catalog, next(iter(catalog.projects))


@pytest.mark.asyncio
async def test_stdio_freeze_tools_share_project_files_and_preserve_human_answer(settings):
    catalog, pid = stepwise(settings)
    settings.transport = "stdio"
    mcp = create_mcp(settings, catalog)
    tools = {tool.name for tool in await mcp.list_tools()}
    assert {
        "freeze_snapshot", "freeze_read_question", "freeze_add_questions", "freeze_change_question"
    } <= tools
    items = [{"group": "结局", "title": "死亡如何处理？", "why": "影响风险解释。",
              "source_summary": "研究方案", "ai_position": "先讨论竞争事件。"}]
    await mcp.call_tool("freeze_add_questions", {
        "project_id": pid, "questions": items, "request_id": "mcp-freeze-round-1"
    })
    store = Freezes(catalog)
    q = store.read(pid, "F-000001")
    assert q["status"] == "open" and q["created_by"] == "chatgpt"
    assert q["ai_position_by"] == "chatgpt"
    await mcp.call_tool("freeze_change_question", {
        "project_id": pid, "question_id": q["id"], "operation": "comment",
        "value": "网页 AI 建议核对日期来源。", "expected_revision": q["revision"],
        "request_id": "mcp-freeze-comment-1",
    })
    assert store.read(pid, q["id"])["messages"][-1]["actor"] == "chatgpt"
    current = store.read(pid, q["id"])
    await mcp.call_tool("freeze_change_question", {
        "project_id": pid, "question_id": q["id"], "operation": "ai_position",
        "value": "Codex 已核对日期实现。", "expected_revision": current["revision"],
        "request_id": "mcp-codex-opinion", "actor": "codex",
    })
    assert store.read(pid, q["id"])["ai_position_by"] == "codex"
    await mcp.call_tool("freeze_add_questions", {
        "project_id": pid, "questions": items, "request_id": "mcp-codex-round", "actor": "codex"
    })
    assert store.read(pid, "F-000002")["created_by"] == "codex"
    with pytest.raises(ToolError):
        await mcp.call_tool("freeze_change_question", {
            "project_id": pid, "question_id": q["id"], "operation": "comment", "value": "冒充用户",
            "expected_revision": 3, "request_id": "mcp-human-actor", "actor": "user",
        })
    with pytest.raises(AccessDenied, match="actor must"):
        store.add(pid, items, "invalid-actor", actor="user")
    with pytest.raises(AccessDenied):
        store.change(pid, q["id"], "answer", "竞争事件", 2, "mcp-answer-forbidden")
    assert (Path(catalog.projects[pid].root) / "project.md").read_text() == "<!-- stepwise-r-project:v3 -->\n"


def test_oauth_freeze_scopes_are_separate_from_governance(settings):
    settings.freeze_mode = "write"
    settings.freeze_projects = [str(settings.projects_file.parent / "projects" / "示例项目")]
    app = create_app(settings)
    app.state.catalog.refresh()
    pid = next(iter(app.state.catalog.projects))
    with TestClient(app, base_url=settings.public_url, follow_redirects=False) as client:
        access = token(client)["access_token"]
        listed = rpc(client, access, "tools/list")["result"]["tools"]
        by_name = {tool["name"]: tool for tool in listed}
        assert by_name["freeze_snapshot"]["securitySchemes"][0]["scopes"] == [SCOPE, FREEZE_READ]
        assert by_name["freeze_add_questions"]["securitySchemes"][0]["scopes"] == [
            SCOPE, FREEZE_READ, FREEZE_WRITE
        ]
        denied = rpc(client, access, "tools/call", {"name": "freeze_snapshot",
            "arguments": {"project_id": pid}})["result"]
        assert denied["isError"] and "mcp/www_authenticate" in denied["_meta"]


def test_freeze_requires_exact_project_allowlist(settings):
    catalog, pid = stepwise(settings)
    store = Freezes(catalog)
    settings.freeze_projects = []
    with pytest.raises(AccessDenied, match="not enabled"):
        store.snapshot(pid)
    settings.freeze_projects = [str(settings.projects_file.parent / "projects" / "another")]
    with pytest.raises(AccessDenied, match="not enabled"):
        store.snapshot(pid)
    settings.freeze_projects = [str(Path(catalog.projects[pid].root))]
    assert store.snapshot(pid)["questions"] == []
